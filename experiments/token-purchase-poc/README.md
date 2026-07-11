# Token-Purchase PoC

A third live-SUT PoC, and a genuinely different kind of test from the first two.
`live-sut-poc` and `complex-sut-poc` both validated the same question: can the
Driver rediscover a bug we deliberately planted and know is there? This one asks
something more real-world: **can genuinely blind exploratory testing surface a real
bug in code neither of us has audited, when we don't know in advance whether one
exists at all?**

`sut.py` was written with ordinary care - not deliberately buggy, but also not
pre-tested by hand before running the harness against it. A "no anomaly found"
result is a real, valid, expected possible outcome here, not a failure of the
harness.

## The scenario

`POST /purchase` - a credit-purchase API backed by a mock payment processor:
`auth_token`, `card_number`, `expiry_month`, `expiry_year`, `cvv`, `credit_count`.
Response: `status` (approved/declined), `decline_reason`, `credits_purchased`,
`total_charged`, `new_credit_balance`, `transaction_id`.

Validation pipeline: auth token resolves to a known user -> that user actually
owns the given card (authorization, not just authentication) -> Luhn check on the
card number -> expiry matches what's on file and hasn't passed -> CVV matches ->
credit_count is sane -> price computed via a tiered bulk-discount schedule -> the
card's spending capacity (see below) covers it -> ledger updated.

Balance updates are protected by a lock around the check-then-commit sequence -
deliberately avoiding the same unsynchronized-shared-state race `complex-sut-poc`'s
rate limiter had, so this run isn't just quietly re-finding the same bug class a
third time.

**Onboarding is fully disclosed, deliberately not a guessing game.** The Driver is
given the schema (including the documented `decline_reason` values - real
published interface facts) and 3 complete, real accounts: `auth_token`,
`card_number`, `expiry_month`, `expiry_year`, and `cvv` for all three - nothing
withheld or left to be reverse-engineered. What's genuinely unknown, and has to be
learned through testing, is each card's spending capacity - never revealed in any
response (realistic: a real payment gateway doesn't tell the merchant a card's
exact available balance either, for the same fraud-prevention reason) - and
whether the many validation rules above actually behave as documented in every
case.

## Architecture

Same unified checkpoint loop as `complex-sut-poc`: every checkpoint proposes and
executes a real batch of tests, forms one hypothesis about behavior and any
anomalies noticed (zero, one, or several), gets a cold Skeptic review, and
continues on "weak" or concludes on "strong_enough" or the checkpoint cap.
`MAX_CHECKPOINTS` is intentionally set to 1 for now, to keep runs cheap while we
confirm the harness and SUT work end to end - raise it once ready for deeper runs.

No `request_count`/`concurrent` test dimension this time - every test is one
ordinary purchase request. Concurrency isn't a dimension of interest here (the
lock exists specifically to close that off).

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

Writes `results/output.json`, `results/bugs.json` (if any anomalies were found -
a list, one entry per anomaly), and `results/report.html` (auto-generated;
`python report.py` regenerates it standalone from an existing `results/`
directory).

Score by hand against `rubric.md`. Since there's no known ground truth this time,
scoring is about whether the process was rigorous (real evidence, genuine
Skeptic pushback, honest hedging) - not "did it find the bug we planted," because
there might not be one.
