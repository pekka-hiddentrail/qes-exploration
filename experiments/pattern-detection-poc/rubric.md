# Scoring rubric

Unlike `hypothesis-poc`, nothing here is pre-flagged as anomalous. The model gets the
raw sequence of 9 calls and has to notice the trend itself, notice which call breaks
it, then hypothesize why. Compare the output against `ground_truth` in
`sequence.json` (withheld from the model) and score:

1. **Pattern found** — does `observed_pattern` correctly describe the length-latency
   correlation across calls 1-8?
   `yes / partial / no`

2. **Outlier found** — is `anomalous_call_index` correctly 9, not some other call?
   `yes / no`

3. **Correct diagnosis** — does `claim` match or reasonably approximate the real
   cause (a length-independent, content-triggered cost - e.g. catastrophic regex
   backtracking), as opposed to just extending the length-based explanation to call 9
   (which would be wrong) or a generic "server load" guess?
   `yes / partial / no`

4. **Discriminating test** — would `confirm_test` / `disconfirm_test` actually tell
   apart "content-specific cause" from "coincidental load spike / network blip", or
   would both explanations survive the test unchanged?
   `yes / no`

5. **Competing explanation quality** — is `competing_explanation` a genuine
   alternative (e.g. transient infra slowness) rather than a strawman?
   `yes / no`

## Notes

| call | pattern found | outlier found | diagnosis | discriminating | competing quality | notes |
|---|---|---|---|---|---|---|
| sequence | | | | | | |
