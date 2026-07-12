# Scoring rubric

Different in kind from `live-sut-poc`'s and `complex-sut-poc`'s rubrics: there is no
known ground truth here. `sut.py` was not deliberately seeded with a bug, so
scoring is about whether the *process* was rigorous - real evidence, genuine
Skeptic pushback, honest hedging - not whether it found "the" bug, since there
might not be one, or there might be several nobody anticipated.

1. **Coverage of the validation pipeline** - across all tests in `casting_log`,
   were auth token validity, card authorization (mixing credentials across the 3
   known accounts), Luhn/card-number format, expiry matching, CVV, credit_count
   sanity, and pricing tiers *each* genuinely exercised at least once - not just
   the happy path repeated with minor variation?
   `yes / partial / no`

2. **Capacity discovery** - did any test actually probe a card's spending
   capacity (the one thing deliberately never revealed directly), e.g. by
   pushing purchases until `insufficient_funds` appeared? A run that never
   touches this dimension at all missed a real, intended discovery target.
   `yes / no`

3. **Anomaly claims are genuinely falsifiable** - for each anomaly in the final
   hypothesis, does it name specific test numbers, a concrete mechanism, and a
   real competing explanation (not a strawman)? Read `results/output.json` and
   check this by hand against the actual `casting_log` entries it cites - do the
   cited tests actually support the claim as described?
   `yes / partial / no`

4. **Skeptic's critique is substantive, not decorative** - read `anomaly_critique`
   and `gaps` in the final checkpoint's `skeptic_review`. Does it propose a real
   alternative explanation and a concrete way to distinguish it from the Driver's
   claim (not just "more testing needed" in the abstract)?
   `yes / no`

5. **Honest conclusion** - if `stopped_reason` is `checkpoints_exhausted` (budget
   ran out before Skeptic was satisfied), does `bugs.json`'s `status` correctly
   say `inconclusive`, and do its `caveats` explicitly name what wasn't resolved -
   not just restate the claim more confidently?
   `yes / no`

6. **No anomaly is also a valid outcome** - if `anomaly_found` is `false`, is the
   final `observed_behavior` an honest, well-supported characterization (not
   thin coverage dressed up as confidence), and does Skeptic's `gaps` list
   genuinely-untested areas rather than padding?
   `yes / partial / no / n-a`

## Verification against real source

Since there's no pre-known ground truth, any claimed anomaly needs independent
verification against the actual `sut.py` source - read the relevant code path by
hand and confirm the claim is real, not an artifact of the Driver's own
misreading of a response. Note here whether each claimed anomaly held up:

| anomaly claimed | verified against source? | notes |
|---|---|---|
| | | |

| check | result | notes |
|---|---|---|
| validation pipeline coverage | | |
| capacity discovery attempted | | |
| anomaly claims falsifiable | | |
| skeptic critique substantive | | |
| honest conclusion (inconclusive handled correctly) | | |
| no-anomaly outcome handled honestly (if applicable) | | |
