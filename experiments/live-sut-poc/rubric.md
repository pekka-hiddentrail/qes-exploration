# Scoring rubric

Same axes as `pattern-detection-poc`, plus a new mechanically-computed one now that
the tests actually run against a live SUT instead of just being described.

Score sections 1-7 only if `anomaly_found` is `true` (an anomaly was actually
discovered during blind casting - there's no fixed "the anomaly is call N", since
casting proposes and executes its own tests with no pre-flagged index). If
`anomaly_found` is `false`, skip to section 8 instead.

1. **Pattern found** — does `observed_pattern` correctly describe the length-latency
   correlation across the baseline calls?
   `yes / partial / no`

2. **Anomaly correctly identified** — does `anomalous_call_index` point at a
   `casting_log` entry whose `actual_latency_class` is genuinely `slow` (not a normal
   result the Driver merely believed was anomalous)?
   `yes / no`

3. **Correct diagnosis** — does `claim` identify a length-independent, content-triggered
   cause (e.g. regex/catastrophic backtracking), rather than extending the length-based
   explanation or guessing generic "server load"? Watch specifically for a hypothesis
   that's *correct* on the mechanism but gets incorrectly walked back later because a
   disconfirm test's own predicted outcome carried a false premise (e.g. assuming
   catastrophic backtracking requires literally repeated identical characters, when a
   broad character-class regex backtracks the same way regardless of repetition) - that
   pattern has happened before and should count against #7, not #3.
   `yes / partial / no`

4. **Competing explanation quality** — genuine alternative, not a strawman?
   `yes / no`

5. **Prediction accuracy (new, mechanically checked)** — read `confirm_result` and
   `disconfirm_result` in `results/output.json`: did `prediction_matched` come back
   `true` for both? This is the first result in this project answering "did the
   proposed test discriminate for real," not just "did it look like it would."
   `confirm: yes/no` · `disconfirm: yes/no`

6. **Skeptic caught something real** — read `skeptic_review` in `results/output.json`
   next to `hypothesis.competing_explanation`. Did `skeptic_alternative` differ
   meaningfully from the investigator's own competing explanation, or just restate
   it? Is `competing_explanation_assessment` (genuine/strawman) a fair call on a
   human read of both?
   `different alternative: yes/no` · `fair strawman call: yes/no`

7. **Skeptic vs. reality** — now that real results exist, was Skeptic's cold-read
   `skeptic_verdict` (holds_up/weak) actually more or less accurate than the
   investigator's own confidence, once the confirm/disconfirm/follow-up results are
   known?
   `skeptic was right / investigator was right / both about equally right`

8. **Behavior-checkpoint quality (only if `anomaly_found` is `false`)** — read
   `behavior_checkpoints` in `results/output.json`. For the final checkpoint's
   `behavior_hypothesis`: is `observed_behavior` an accurate characterization given
   the real `casting_log`, not overclaiming beyond what was actually tested? Did the
   behavior-Skeptic's `gaps` name real, non-trivial untested areas rather than
   restating what's already covered? If more than one checkpoint ran, did the later
   checkpoint's tests visibly address the earlier checkpoint's flagged gaps?
   `hypothesis accurate: yes/partial/no` · `gaps were real: yes/no` · `gaps addressed
   next checkpoint: yes/no/n-a`

## Notes

**Bug report quality (only if `anomaly_found` is `true`)** — read `results/bugs.json`.
Beyond whether the symptom/threshold/severity are right, check whether the stated
root cause is actually correct against `sut.py` (not just internally consistent with
the investigation's own data) - a report can nail the reproducible symptom and still
state the wrong mechanism if an earlier disconfirm test's false premise steered the
investigation away from the true cause.

Pay special attention to *why* a prediction failed if it did - a wrong
`predicted_latency_class` for the confirm test (which should be unsurprising) is a
different kind of miss than a wrong prediction for the disconfirm test (which is
supposed to be the one that could go either way and still be informative).

| check | result | notes |
|---|---|---|
| pattern found | | |
| anomaly correctly identified | | |
| diagnosis | | |
| competing quality | | |
| confirm prediction matched | | |
| disconfirm prediction matched | | |
| skeptic found different alternative | | |
| skeptic's strawman call was fair | | |
| skeptic vs. reality | | |
| bug report root cause correct (if found) | | |
| behavior hypothesis accurate (if not found) | | |
| behavior-Skeptic gaps were real (if not found) | | |
| gaps addressed next checkpoint (if not found) | | |
