# Scoring rubric

For each case, compare the model's output against `ground_truth` (which was withheld
from the model) and score these four questions. This is scored by hand for now.

1. **Correct diagnosis** — does `claim` match or reasonably approximate the ground
   truth, as opposed to a plausible-but-wrong explanation?
   `yes / partial / no`

2. **Discriminating test** — would `confirm_test` and `disconfirm_test` actually
   produce *different* outcomes depending on whether `claim` or
   `competing_explanation` is true? Or would both pass regardless (the failure mode
   the concept doc calls out)?
   `yes / no`

3. **Self-aware discrimination** — does `why_this_discriminates` correctly explain
   the difference in predicted outcomes, or is it a rationalization that doesn't
   actually hold up?
   `yes / no`

4. **Competing explanation quality** — is `competing_explanation` a genuine,
   reasonable alternative a careful engineer would consider, or a strawman?
   `yes / no`

## Notes column

Leave a one-line note per case on anything surprising — especially cases where the
model's hypothesis sounds confident and well-reasoned but is simply wrong, since that
is the specific risk this test exists to surface.

| case id | 1. diagnosis | 2. discriminating | 3. self-aware | 4. competing quality | notes |
|---|---|---|---|---|---|
| case-01-cancel-status-mismatch | | | | | |
| case-02-promo-discount-inconsistency | | | | | |
| case-03-soft-delete-not-applied | | | | | |
| case-04-200-on-validation-error | | | | | |
| case-05-search-latency-spike | | | | | |
