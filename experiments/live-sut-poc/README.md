# Live-SUT PoC

`pattern-detection-poc` tested reasoning against a hand-typed, mocked call sequence.
This closes a real gap: it never actually ran the disconfirm test it proposed. This
version stands up a real local server with a genuine bug, and the harness gives
Claude ("the Driver") zero pre-flagged anomaly - it has to discover a real bug blind,
form falsifiable hypotheses, get them critiqued by a separate cold "Skeptic", execute
confirm/disconfirm tests for real against the live SUT, and write a bug report - so we
can check whether the real outcome matches the prediction, not just whether the
process *looks* rigorous.

## How it works

**Phase A - blind casting.** The Driver gets 8 baseline calls (normal latency scaling
with input length, nothing flagged) and proposes batches of tests: a mix of specific,
falsifiable hypotheses and pure edge-case probes. All tests in a batch execute for
real before the Driver sees any results. Casting is broken into checkpoints (bounded
rounds each); if a checkpoint's rounds end without finding anything, the Driver is
forced to write a **behavior hypothesis** - not "what's the bug" (there may not be
one), but "what have I learned and what's still untested" - and an independent cold
**Skeptic** critiques it, naming concrete gaps. That critique feeds into the next
checkpoint's tests (the Driver still decides what to actually test, not the critique
itself). This guarantees the Skeptic gets exercised at least once per run even when
nothing is ever found, and keeps the search directed rather than repeating the same
kind of guess for the whole budget.

**Phase B - once something's found.** The moment any test comes back genuinely slow,
casting stops immediately and hands off to: a specific, falsifiable `claim` +
competing explanation -> a separate Skeptic cold-review of *that* claim (proposing
disproof strategies, never writing tests itself) -> real confirm/disconfirm tests
executed against the live SUT -> up to two follow-up rounds refining the verdict ->
a bug report written to `results/bugs.json` (title, repro steps, severity, honest
caveats about what wasn't resolved). Claude designs the literal request text for
confirm/disconfirm/follow-up tests itself.

## Risk note

`sut.py`'s `/analyze` endpoint contains a genuine, intentional exponential-time
vulnerability (Python `re` catastrophic backtracking on `^([a-zA-Z]+)+$` - any run of
letters, any mix of case, not tied to a specific character), and has no
timeout/kill-switch of its own by design - it's meant to be naively vulnerable, and
this only ever runs locally, for one person, on purpose.

The harness itself does guard against this: an early run actually hit the failure
mode this note warns about - a proposed test used a longer alphabetic run than
calibrated and hung the server for over 60 seconds (blowup is exponential, so a few
more characters than expected turns a few-second request into a multi-minute one).
`run_live.py` refuses to execute any test whose text has an alphabetic run longer than
`MAX_SAFE_ALPHA_RUN` (25 characters) and records it as `skipped` instead. If you
manually craft a request to this endpoint outside the harness, that guard doesn't
apply - don't send longer alphabetic runs.

**Detecting "slow" isn't a flat threshold.** A flat cutoff can't distinguish
"genuinely pathological" from "long but ordinary input" (the SUT's normal cost model
is linear in length). Instead, the harness fits a real linear regression
(`fit_baseline_latency_model`) to the 8 baseline calls to separate the fixed
per-request overhead from the true marginal per-character cost, then classifies a
result "slow" only if it's `SLOW_MULTIPLIER`x (5x) that model's prediction, with
`SLOW_THRESHOLD_MS` (150ms) as an absolute floor so short inputs aren't flagged on
noise. Both numbers were tuned against a real miss: an earlier calibration (a flat
500ms floor, and a "worst observed ratio" baseline rate instead of a real regression)
let a genuinely anomalous, escalating result (65ms -> 184ms -> 338ms as input length
grew) go undetected the whole time, even though the Driver's own comparative
reasoning had already recognized the pattern in its behavior hypothesis.

## Run it

Two terminals:

```
# terminal 1
pip install -r requirements.txt
uvicorn sut:app --port 8000

# terminal 2
cp .env.example .env   # fill in ANTHROPIC_API_KEY
python run_live.py
```

Writes two files under `results/` (gitignored):
- `output.json` - the full run: baseline calls, every casting round's reasoning and
  executed tests (tagged by checkpoint/round), `behavior_checkpoints` (behavior
  hypothesis + Skeptic critique for any checkpoint that found nothing), and if an
  anomaly was found: the claim, Skeptic review, confirm/disconfirm results, and every
  follow-up round.
- `bugs.json` - present only if an anomaly was found: the final bug report (title,
  description, repro steps, expected vs. actual behavior, severity, and caveats about
  anything left unresolved).

Score by hand against `rubric.md`.
