# Scoring rubric

Same spirit as `live-sut-poc`'s rubric, adapted for a response-content-based
anomaly (accepted request count vs. disclosed limit) instead of a latency-based
one, and for a bug that specifically requires testing concurrency, not just
request content.

Score sections 1-7 only if `anomaly_found` is `true`. If `anomaly_found` is
`false`, skip to section 8 instead.

1. **Concurrency considered at all** - did the Driver's own reasoning explicitly
   raise the possibility of testing concurrent requests (not just varying
   content/client_id) before or during the round that found the anomaly? A run
   that stumbles onto `concurrent=true` without ever articulating *why* is a
   weaker result than one that reasons "sequential testing can't reveal a
   concurrency bug" and deliberately designs a concurrent test on that basis.
   `yes / partial / no`

2. **Anomaly correctly identified** - does `anomalous_test_number` point at a
   `casting_log` entry whose `actual_correctness` is genuinely `overcounted`
   (not a normal result the Driver merely believed was anomalous)?
   `yes / no`

3. **Correct diagnosis** - does `claim` identify the actual mechanism (a
   check-then-act race / non-atomic read-modify-write on shared state), not just
   "the rate limit is broken" or a guess unconnected to concurrency?
   `yes / partial / no`

4. **Competing explanation quality** - genuine alternative, not a strawman (e.g.
   "multiple server instances with independent local counters" is a fair rival;
   "it's random" is weaker but still not a strawman)?
   `yes / no`

5. **Disconfirm test actually disconfirms** - check `disconfirm_result.request`:
   if the claim is concurrency-specific, does the disconfirm test genuinely use
   `concurrent: false` with `request_count > 1` (a real sequential test), not a
   single request or (bug seen once already) a mislabeled concurrent burst? A
   disconfirm test that can't actually distinguish the claim from its rivals
   isn't a real test of it, regardless of what it predicted.
   `yes / no`

6. **Prediction accuracy (mechanically checked)** - did `prediction_matched` come
   back `true` for both `confirm_result` and `disconfirm_result`?
   `confirm: yes/no` · `disconfirm: yes/no`

7. **Skeptic caught something real** - read `skeptic_review` next to
   `hypothesis.competing_explanation`. Did `skeptic_alternative` differ
   meaningfully from the investigator's own competing explanation? Is
   `competing_explanation_assessment` a fair call on a human read of both?
   `different alternative: yes/no` · `fair strawman call: yes/no`

8. **Behavior-checkpoint quality (only if `anomaly_found` is `false`)** - read
   `behavior_checkpoints`. Does `observed_behavior` accurately reflect what was
   actually tested? Did the behavior-Skeptic's `gaps` include the concurrency
   dimension specifically (i.e. did it notice that everything tested so far was
   sequential, if that's true)? If more than one checkpoint ran, did the later
   checkpoint's tests visibly address earlier gaps?
   `hypothesis accurate: yes/partial/no` · `gaps named concurrency: yes/no` ·
   `gaps addressed next checkpoint: yes/no/n-a`

## Notes

**Bug report quality (only if `anomaly_found` is `true`)** - read
`results/bugs.json`. Beyond whether the symptom and severity are right, check
`caveats` for honesty about what black-box testing genuinely can't distinguish
(e.g. an in-process threading race vs. per-worker counter isolation vs. a
storage-layer atomicity gap) - a report that confidently claims a specific
implementation-level root cause without having inspected the code is
overclaiming, and that should count against this axis even if the symptom and
severity are otherwise correct.

Pay attention to *why* a `request_count > 1` test with `concurrent: true` was
proposed at all - was it because the Driver reasoned about shared state and
concurrency safety, or because it was mimicking a burst test's shape without a
clear rationale? The former is a genuine disconfirmation-engine behavior; the
latter is closer to a lucky guess that happened to use the right tool.

| check | result | notes |
|---|---|---|
| concurrency considered | | |
| anomaly correctly identified | | |
| diagnosis | | |
| competing quality | | |
| disconfirm actually disconfirms | | |
| confirm prediction matched | | |
| disconfirm prediction matched | | |
| skeptic found different alternative | | |
| skeptic's strawman call was fair | | |
| bug report root cause honest about limits (if found) | | |
| behavior hypothesis accurate (if not found) | | |
| gaps named concurrency (if not found) | | |
| gaps addressed next checkpoint (if not found) | | |
