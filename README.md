# qes-exploration

[![engine tests](https://github.com/pekka-hiddentrail/qes-exploration/actions/workflows/engine-tests.yml/badge.svg)](https://github.com/pekka-hiddentrail/qes-exploration/actions/workflows/engine-tests.yml)

An LLM-based **disconfirmation engine** for exploratory API testing: instead of
running a fixed, pre-scripted test plan, it drives a live system, forms a
falsifiable hypothesis about its behavior, and puts that hypothesis through a
cold, adversarial review before trusting it - mirroring how real scientific
method works rather than how most AI-testing tools work (which mostly confirm,
rarely try hard to disprove themselves). See
[`docs/exploratory-testing-engine-concept.md`](docs/exploratory-testing-engine-concept.md)
for the original vision this project is one deliberately narrow, implemented
slice of.

## How a run works

Against a live system under test (SUT), each **checkpoint**:

1. **Casts** a batch of real tests (an adapter-defined test-proposal schema),
   executes them for real, and records predicted vs. actual outcomes.
2. Forms **one hypothesis** about the system's behavior and any anomalies
   noticed - a specific, falsifiable claim per anomaly, not a vague suspicion.
3. Gets a **cold Skeptic review** of that hypothesis: a second LLM call that
   never sees the raw test data, only the hypothesis itself. It checks
   whether the cited evidence actually discriminates the claim from its own
   named rival explanation - not just whether evidence exists - and returns a
   `weak` (keep going) or `strong_enough` (stop) verdict.
4. The loop continues on `weak`, informed by the Skeptic's critique, or stops
   on `strong_enough` or a checkpoint cap.

If the final hypothesis claims anomalies, a bug report is written per claim -
honestly marked `inconclusive` if the checkpoint budget ran out while the
Skeptic still had objections, `corroborated` only if it was satisfied. Output
is a JSON result, a JSON bug list, and a self-contained HTML report.

## Bootstrapping a new adapter automatically

Testing a new API normally means hand-writing an *adapter* (see below). The
`engine/bootstrap/` pipeline can generate a first draft of one instead, by
actually pointing itself at a live system and working out the schema for real:

```
Discover      → try the live SUT's own OpenAPI/Swagger doc first (free, exact)
Draft (LLM)   → only if discovery found nothing: infer a schema from free text
Probe (LLM)   → send real requests to confirm/correct the draft against
                 the live system's actual responses - a 422 naming a missing
                 field resolves an unknown more reliably than a guess would
Generate      → emit a real, runnable adapter.py from what was confirmed
```

Run all four phases end to end with:

```
python -m engine.bootstrap.cli \
  --name my_api --display-name "My API" --base-url http://localhost:8000
```

This never auto-registers or auto-runs the result - it prints the line to add
to `engine/adapters/registry.py` and the command to run it, keeping
registration a deliberate human step. A generated adapter that never achieved
a real success is refused outright (`status == "failed"`); one that ran out
of probing budget while still uncertain is generated anyway, with a
prominent warning comment carrying forward exactly what's still unconfirmed.
See [`docs/examples/bootstrap_demo/`](docs/examples/bootstrap_demo/) for a
real, unedited run of this pipeline - including the adapter it generated and
the bug it found.

## Layout

```
engine/
  adapter.py    # SUTAdapter interface - what a per-SUT adapter must supply
  tools.py      # HYPOTHESIS_TOOL / SKEPTIC_TOOL / BUG_REPORT_TOOL - shared across every adapter
  client.py     # Anthropic client + call_tool_with_retry (tool-forced calls, retried on transient errors)
  loop.py       # the checkpoint loop itself
  report.py     # generic HTML rendering (prose, badges, CSS, page/checkpoint structure)
  runner.py     # orchestrates one full run: readiness probe, loop, bug reports, file output
  cli.py        # python -m engine.cli --adapter <name>
  adapters/
    registry.py           # name -> adapter module, resolved lazily at run time
    token_purchase/        # first adapter: single request/response, decline-reason logic
    complex_sut/            # second adapter: concurrency/rate-limiting, proves the interface generalizes
  bootstrap/
    discovery.py  # Phase 1 - fetch and parse a live OpenAPI/Swagger document
    freetext.py   # Phase 2 - LLM fallback: infer a schema from free-text spec text
    schema.py     # ties discovery and the free-text fallback together
    probe.py      # Phase 3 - active probing loop against the live SUT
    generate.py   # Phase 4 - generate a real adapter.py from a confirmed/inconclusive result
    cli.py        # python -m engine.bootstrap.cli - chains all 4 phases end to end
  tests/          # deterministic regression + parity tests (no LLM calls, runs in CI)
experiments/      # earlier throwaway prototypes this package was hardened from - untouched historical archive
docs/
  exploratory-testing-engine-concept.md  # the original, broader vision
  examples/bootstrap_demo/                # a real worked example of the bootstrap pipeline's output
```

`engine/*` never imports from `engine/adapters/*` - adapters import from
`engine`, never the reverse. `engine/adapters/registry.py` is the only place
that crosses that boundary, and it does so lazily (`importlib`) at CLI run
time. `engine/bootstrap/` follows the same rule: it depends on `engine/`, not
on any concrete adapter.

## Getting started

```
# terminal 1
pip install -r engine/requirements.txt
uvicorn engine.adapters.token_purchase.sut:app --port 8000

# terminal 2
cp engine/.env.example engine/.env   # fill in ANTHROPIC_API_KEY
python -m engine.cli --adapter token_purchase
```

Writes `runs/<adapter>/output.json`, `runs/<adapter>/bugs.json` (if any
anomalies were found), and `runs/<adapter>/report.html`. Override run
parameters with `--model`, `--max-checkpoints`, `--first-round-budget`,
`--default-budget`, `--out-dir`.

See [`engine/README.md`](engine/README.md) for adding a new adapter by hand,
and the CI/testing setup.

## Testing

```
pip install -r engine/requirements.txt
python -m pytest engine/tests
```

Runs automatically on every push to `master` and every PR via
[`.github/workflows/engine-tests.yml`](.github/workflows/engine-tests.yml) -
no Anthropic API key needed, since no test makes a real LLM call.
