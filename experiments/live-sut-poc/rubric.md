# Scoring rubric

Same axes as `pattern-detection-poc`, plus a new mechanically-computed one now that
the tests actually run against a live SUT instead of just being described.

1. **Pattern found** — does `observed_pattern` correctly describe the length-latency
   correlation across calls 1-8?
   `yes / partial / no`

2. **Outlier found** — is `anomalous_call_index` correctly 9?
   `yes / no`

3. **Correct diagnosis** — does `claim` identify a length-independent, content-triggered
   cause (e.g. regex/catastrophic backtracking), rather than extending the length-based
   explanation or guessing generic "server load"?
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

## Notes

Pay special attention to *why* a prediction failed if it did - a wrong
`predicted_latency_class` for the confirm test (which should be unsurprising) is a
different kind of miss than a wrong prediction for the disconfirm test (which is
supposed to be the one that could go either way and still be informative).

| check | result | notes |
|---|---|---|
| pattern found | | |
| outlier found | | |
| diagnosis | | |
| competing quality | | |
| confirm prediction matched | | |
| disconfirm prediction matched | | |
| skeptic found different alternative | | |
| skeptic's strawman call was fair | | |
| skeptic vs. reality | | |
