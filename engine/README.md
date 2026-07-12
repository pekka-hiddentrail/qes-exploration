# AI Exploratory Testing Engine

A reusable Driver+Skeptic checkpoint-loop harness, hardened from four rounds
of experimentation in `experiments/` (kept there as an untouched historical
archive - this package is a port, not a rewrite). See
`docs/exploratory-testing-engine-concept.md` for the original vision this is
one deliberately narrow slice of.

## What it does

Against a live SUT, each checkpoint:
1. **Casts** a batch of real tests (an adapter-defined test-proposal schema),
   executes them for real, and records predicted vs. actual outcomes.
2. Forms **one hypothesis** about the system's behavior and any anomalies
   noticed (zero, one, or several) - a specific, falsifiable claim per
   anomaly, not a vague suspicion.
3. Gets a **cold Skeptic review** of that hypothesis - a second LLM call that
   never sees the raw test data, only the hypothesis itself. It checks
   whether the cited evidence actually discriminates a claim from its own
   named rival (not just whether evidence exists), tracks whether its own
   prior critique was actually addressed across checkpoints, and gives a
   `weak` (keep going) or `strong_enough` (stop) verdict.
4. The loop continues on `weak`, informed by the critique, or stops on
   `strong_enough` or a checkpoint cap.

If the final hypothesis claims anomalies, a bug report is written per claim -
honestly marked `inconclusive` if the checkpoint budget ran out while the
Skeptic still had objections, `corroborated` only if it was satisfied.

**Known, accepted limitation:** the Skeptic's `inference_validity_check`
doesn't account for realistic value rounding/precision when deciding whether
cited evidence discriminates a claim from its rival - see the comment on
that field in `engine/tools.py`. Carried forward deliberately, not fixed.

## Layout

```
engine/
  adapter.py    # SUTAdapter interface - what a per-SUT adapter must supply
  tools.py      # HYPOTHESIS_TOOL / SKEPTIC_TOOL / BUG_REPORT_TOOL - domain-agnostic, not adapter-overridable
  client.py     # Anthropic client + call_tool_with_retry
  loop.py       # the checkpoint loop itself
  report.py     # generic HTML rendering (prose, badges, CSS, page/checkpoint structure)
  runner.py     # orchestrates one full run: readiness probe, loop, bug reports, file output
  cli.py        # python -m engine.cli --adapter <name>
  adapters/
    registry.py           # name -> adapter module, resolved lazily
    token_purchase/        # first adapter, ported from experiments/token-purchase-poc
  tests/                    # deterministic regression + parity tests (no LLM calls)
```

`engine/*` never imports from `engine/adapters/*` - adapters import from
`engine`, never the reverse. `engine/adapters/registry.py` is the only place
that crosses that boundary, and it does so lazily (`importlib`) at CLI run
time.

## Running it

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

## Adding a new adapter

1. Create `engine/adapters/<name>/` with your mock SUT and an `adapter.py`.
2. In `adapter.py`, define the genuinely per-SUT pieces and build one
   `ADAPTER = SUTAdapter(...)` instance - see
   `engine/adapters/token_purchase/adapter.py` for a complete worked example.
   Required fields: `name`, `display_name`, `base_url`, `test_endpoint_path`,
   `casting_tool_schema`, `casting_system_prompt`, `validate_casting_response`,
   `execute_test`, `render_test_entry`, `render_onboarding_section`.
   `validate_adapter()` (in `engine/adapter.py`) checks these are present and
   raises a clear error naming what's missing, before any HTTP/Anthropic
   calls are made.
3. Register it in `engine/adapters/registry.py`'s `_ADAPTERS` map.
4. Do **not** touch `engine/tools.py` - the hypothesis/Skeptic schema is
   shared across every adapter by design.

## Testing

```
pip install -r engine/requirements.txt
python -m pytest engine/tests
```

`test_sut_regression.py` and `test_client_retry.py` make no Anthropic calls
and run in-process against the mock SUT via FastAPI's `TestClient`.
