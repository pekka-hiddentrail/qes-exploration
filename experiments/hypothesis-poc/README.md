# Hypothesis-generation PoC

Tests the one genuinely uncertain piece of `docs/exploratory-testing-engine-concept.md`
in isolation: given an anomaly (pre-flagged, not detected by the model), can Claude
produce a correct hypothesis and a confirm/disconfirm test pair that actually
discriminates between explanations - rather than a test that looks rigorous but would
pass either way?

No loop, no live SUT, no oracles. Just a static dataset of 5 hand-built REST
request/response anomalies (`cases.json`) run through one prompt.

## Run it

```
pip install -r requirements.txt
cp .env.example .env   # fill in ANTHROPIC_API_KEY
python run.py
```

Writes `results/output.json` (gitignored). Score each case by hand against
`rubric.md`.

## Files

- `cases.json` - the 5 seeded anomalies. Each has `request`/`response`/`context`,
  `expected` vs `observed`, and a `ground_truth` field that is withheld from the model
  and used only for scoring.
- `run.py` - sends each case's evidence (minus `ground_truth`) to Claude, forces
  structured output via tool use, saves results.
- `rubric.md` - the scoring checklist and results table.
