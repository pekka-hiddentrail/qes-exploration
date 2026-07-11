# Complex-SUT PoC

`live-sut-poc` used a single-request bug (catastrophic regex backtracking) where
sequential testing was always enough to eventually stumble onto the trigger. This
one is deliberately harder: a genuine TOCTOU (check-then-act) race condition in
per-client rate limiting, which sequential testing - no matter how thorough - can
never reveal at all. Finding it requires the Driver to realize on its own that
concurrency is a dimension worth testing, not just request content.

Onboarding is also different: instead of 8 pre-baked baseline calls establishing
an empirical latency pattern to infer, the Driver is given the API's schema
documentation (what a real tester reading published docs would know) plus exactly
one real "happy day" call, then has to design and execute its own tests from
there - including discovering on its own whether concurrency matters.

## How it works

Same checkpoint-cycle architecture as `live-sut-poc`: casting is broken into
checkpoints (bounded rounds each); if a checkpoint's rounds find nothing, the
Driver writes a behavior hypothesis and an independent cold Skeptic critiques it,
feeding forward into the next checkpoint. The moment any test shows more accepted
requests than the disclosed rate limit allows, casting stops and hands off to
formal hypothesis formation, a separate Skeptic review, confirm/disconfirm
execution, a bounded follow-up loop, and a bug report - all unchanged in spirit
from `live-sut-poc`.

**The one new test dimension**: every test specifies `request_count` and
`concurrent` (true/false), not just a single request. `concurrent=true` fires all
`request_count` requests at the same moment - the only way to test whether shared
server-side state is safe under real concurrent access. `concurrent=false` sends
them one at a time, each waiting for the previous response - a genuine sequential
test, distinct from (and not replaceable by) a single request. This distinction
matters a lot in practice: an early run's disconfirm test used a *concurrent*
burst of 10 while believing it was testing "sequential" behavior (calling it
`request_count=10` alone, with no way to specify execution mode at all) - it got
overcounted results, and the model concluded its own (correct) race-condition
hypothesis was refuted. That was a harness bug, not a reasoning failure - fixed by
making `concurrent` an explicit, required field on every test.

## Risk note

`sut.py`'s `/submit` endpoint contains a genuine, intentional TOCTOU race
condition (a non-atomic read-check-write on a shared dict, with a small
artificial processing delay between the check and the write that makes the race
reliably exploitable over a real network instead of needing microsecond-precision
timing). FastAPI runs sync handlers in a thread pool and `time.sleep()` releases
the GIL, so concurrent requests genuinely interleave - this is a real Python
threading race, not simulated.

Unlike `live-sut-poc`'s vulnerability, nothing here can hang: each request is a
bounded ~50ms of simulated work regardless of concurrency. `run_live.py` still
refuses any test whose `request_count` exceeds `MAX_REQUEST_COUNT` (20), purely
to bound resource usage and result size, not to prevent a hang.

**Window duration matters more than it looks.** `WINDOW_SECONDS` was originally
10s and looked fine in isolation, but a real investigation session involves many
Claude API round-trips (each taking real wall-clock seconds), so two sequential
test executions several rounds apart can easily be 10+ seconds apart in real
time - long enough for a 10s window to spuriously reset between them. That
produced a misleading "the counter doesn't persist across requests" artifact that
had nothing to do with the actual bug. Set to 600s to give a comfortable margin
for a realistic investigation's total wall-clock duration.

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

Writes three files under `results/` (gitignored):
- `output.json` - the schema doc, the one happy-day example, every casting
  round's reasoning and executed tests (tagged by checkpoint/round),
  `behavior_checkpoints` (behavior hypothesis + Skeptic critique for any
  checkpoint that found nothing), and if an anomaly was found: the claim,
  Skeptic review, confirm/disconfirm results, and every follow-up round.
- `bugs.json` - present only if an anomaly was found: the final bug report.
- `report.html` - the same content rendered as a single self-contained,
  human-readable page instead of raw JSON. Generated automatically at the end
  of every run by `report.py`; rerun `python report.py` standalone to
  regenerate it from an existing `results/` directory without a fresh live run.

Score by hand against `rubric.md`.
