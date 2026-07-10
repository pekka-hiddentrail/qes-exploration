# Pattern-detection PoC

`hypothesis-poc` tested reasoning given a pre-flagged anomaly. This tests the step
before that: given a raw, unflagged sequence of calls, can Claude notice a pattern
itself, notice which call breaks it, and still produce a good hypothesis and a
discriminating confirm/disconfirm test?

Still fully synthetic/mocked - no live SUT, no real network calls except to the
Anthropic API itself.

## Scenario

`sequence.json` - 9 calls to a mocked `POST /analyze` endpoint. Calls 1-8 show
latency tracking input text length (longer text, longer latency - and vice versa).
Call 9 is a similar length to several earlier calls, but its latency rockets: its
text contains a character run designed to trigger catastrophic backtracking in an
input-validation regex, a cause unrelated to length. `ground_truth` explains this and
is withheld from the model.

## Run it

```
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
python run.py
```

Writes `results/output.json` (gitignored). Score it by hand against `rubric.md`.
