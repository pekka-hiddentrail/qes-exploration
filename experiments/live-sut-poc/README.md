# Live-SUT PoC

`pattern-detection-poc` tested reasoning against a hand-typed, mocked call sequence.
This closes a real gap: it never actually ran the disconfirm test it proposed. This
version stands up a real local server with the same bug for real, and the harness
actually executes Claude's proposed confirm/disconfirm requests against it - so we can
check whether the real outcome matches the prediction, not just whether the test
*looks* well-designed.

## Risk note

`sut.py`'s `/analyze` endpoint contains a genuine, intentional exponential-time
vulnerability (Python `re` catastrophic backtracking on `^(a+)+$`), and has no
timeout/kill-switch of its own by design - it's meant to be naively vulnerable, and
this only ever runs locally, for one person, on purpose.

The harness itself, however, does guard against this: Claude designs its own
confirm/disconfirm test text, and an early run of this PoC actually hit the failure
mode this note warns about - a proposed test used a longer repeated-character run
than our calibrated example and hung the server for over 60 seconds (blowup is
exponential, so a few more characters than expected turns a few-second request into a
multi-minute one). `run_live.py` now refuses to execute any test whose text has a
repeated-character run longer than the one calibrated example (25 'a's + '!', ~3.6s)
and records it as `skipped` instead. If you manually craft a request to this endpoint
outside the harness, that guard doesn't apply - don't send longer repeated runs.

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

Writes `results/output.json` (gitignored): the real call log, Claude's hypothesis,
and the real executed outcome of both the confirm and disconfirm test, including
whether each prediction matched reality (`prediction_matched`). Score by hand against
`rubric.md`.
