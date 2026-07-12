# Adapter-bootstrap demo: token_purchase

A real, unedited run of the full 4-phase adapter-bootstrap pipeline against
the `token_purchase` mock SUT (`engine/adapters/token_purchase/sut.py`),
followed by a real 3-checkpoint run of the Driver+Skeptic checkpoint loop
against the generated adapter. Kept here as a worked example of what the
pipeline actually produces - not part of the runnable engine.

## How this was produced

```
python -m engine.bootstrap.cli \
  --name bootstrap_demo_purchase --display-name "Bootstrap Demo: Purchase" \
  --base-url http://127.0.0.1:8020 --max-probes 8

# registered the printed line in engine/adapters/registry.py, then:
python -m engine.cli --adapter bootstrap_demo_purchase --max-checkpoints 3
```

## Files

- **`generated_adapter.py`** - the draft `adapter.py` written by
  `engine.bootstrap.generate`, with zero hand edits. Bootstrap status came
  back `inconclusive` (see the warning comment at the top) - the probing
  loop confirmed the request/response schema via real 422s, but never
  discovered a valid `auth_token` in 8 probes, since the mock SUT gates on
  a fixed value nothing short of already knowing it would guess.
- **`report.html`** - the full HTML report from the 3-checkpoint run
  against that generated adapter, rendered with the generated
  `render_test_entry`/`render_onboarding_section` hooks.
- **`bugs.json`** - the one bug report the run produced.
- **`output.json`** - the raw checkpoint-by-checkpoint log (all 28 tests,
  hypotheses, Skeptic reviews) the report was rendered from.

## What it actually found

Every one of the 28 tests - spanning missing/invalid `auth_token` values,
boundary and type-coercion probes on the other fields, and 25+ guessed
token candidates - got back `HTTP 200` with the failure encoded only in
the JSON body (`{"status": "declined", "decline_reason":
"invalid_auth_token", ...}`), never a `401`/`403`. The Driver flagged this
as a real finding (callers checking only the HTTP status code would treat
an auth failure as success); the Skeptic marked it `weak`/`inconclusive`
because the budget ran out before the same question could be answered for
any non-auth decline path, and header-based auth delivery
(`Authorization`/`X-API-Key`) was never tried.

Honest limitation worth calling out: because the real auth token was never
found, the entire business-logic surface of this API (card validation,
pricing, the actual success path) went completely untested in this run.
That's expected, not a bug in the tool - it's what "inconclusive" is
supposed to mean, and it's a fair illustration of where blind auto-bootstrap
hits a real wall (a secret nothing in the schema or free text reveals).
