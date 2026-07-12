"""HYPOTHESIS_TOOL, SKEPTIC_TOOL, BUG_REPORT_TOOL: the domain-agnostic core of
the checkpoint loop, ported verbatim from token-purchase-poc's most-evolved
version (the one that survived several deliberate hardening passes -
inference-validity checking, recommended-next-tests, prior-critique
continuity tracking). These are NOT adapter-overridable: their wording never
references domain nouns (card numbers, credit counts, etc.) - only "test
numbers," "anomaly claims," "rival explanations" - so there is no present
need to let a per-SUT adapter drift them.
"""

HYPOTHESIS_TOOL = {
    "name": "submit_checkpoint_hypothesis",
    "description": "Characterize the system's behavior based on real test results so far, including any anomalies (possible bugs) noticed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_behavior": {
                "type": "string",
                "description": "General characterization: confirmed patterns, categories tested and found normal.",
            },
            "anomalies": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Zero or more possible bugs noticed. Each entry should be a specific, falsifiable claim "
                    "in its own words - which test number(s) revealed it, what you believe the mechanism is, "
                    "how severe it would be if true, and a genuine competing explanation for the same "
                    "observation (not a strawman you'd easily dismiss). Leave empty if nothing anomalous has "
                    "been found yet - don't force a claim that isn't there. It's entirely possible this "
                    "implementation has no bugs at all."
                ),
            },
            "untested_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 1-2 things not yet tried, worth trying next.",
                "minItems": 1,
            },
            "prior_gaps_response": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Only relevant if your evidence includes 'prior_skeptic_review' (a prior checkpoint's "
                    "Skeptic critique naming gaps/recommended_next_tests). For EACH one it named, say "
                    "plainly one of: it was tested this checkpoint (cite the test number), it's untestable "
                    "with the current scenario data (state exactly why - e.g. 'no known account has two "
                    "cards, so this can't be constructed'), it's already conclusively resolved by existing "
                    "evidence (state why no further test could add anything), or it's simply not yet "
                    "attempted (a genuine remaining gap). Don't declare something untestable or resolved "
                    "just to avoid testing it - the Skeptic will judge whether your stated reason actually "
                    "holds up. Leave empty if there was no prior review (first checkpoint)."
                ),
            },
        },
        "required": ["observed_behavior", "anomalies", "untested_areas", "prior_gaps_response"],
    },
}

HYPOTHESIS_SYSTEM_PROMPT = """You are characterizing this system's behavior based on real test results
from this session so far. Describe the general behavior pattern, and list any anomalies (possible
bugs) you've noticed - each as a specific, falsifiable claim referencing the test number(s) that
revealed it, your best guess at the mechanism, its severity if true, and a genuine rival explanation
for the same observation (not a strawman). If nothing anomalous has turned up yet, leave anomalies
empty rather than forcing a claim that isn't there - this implementation may genuinely have no bugs.
List what's still untested.

If your evidence includes 'prior_skeptic_review', that Skeptic named specific gaps and recommended
tests last checkpoint. Fill in prior_gaps_response addressing each one directly: tested (cite the test
number), untestable with the current scenario data (say exactly why, concretely - not just "couldn't
get to it"), already conclusively resolved (say why no further test would change anything), or not yet
attempted. Be honest here - claiming something is untestable or resolved when it isn't will be judged
by the Skeptic against your stated reason, not taken on faith.

Call submit_checkpoint_hypothesis with your answer."""


def validate_hypothesis_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("observed_behavior", "anomalies", "untested_areas", "prior_gaps_response"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    anomalies = data.get("anomalies")
    if not isinstance(anomalies, list) or not all(isinstance(a, str) for a in anomalies):
        errors.append("'anomalies' must be a list of strings (may be empty)")
    areas = data.get("untested_areas")
    if not isinstance(areas, list) or not areas or not all(isinstance(a, str) for a in areas):
        errors.append("'untested_areas' must be a non-empty list of strings")
    prior_gaps = data.get("prior_gaps_response")
    if not isinstance(prior_gaps, list) or not all(isinstance(g, str) for g in prior_gaps):
        errors.append("'prior_gaps_response' must be a list of strings (may be empty)")
    return errors


SKEPTIC_TOOL = {
    "name": "submit_skeptic_review",
    "description": "Cold-review a checkpoint hypothesis - you have NOT seen the underlying test data. Poke holes in it; don't rubber-stamp it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["weak", "strong_enough"],
                "description": "'weak' only for a MATERIAL reason: an overconfident behavior characterization, an anomaly claim that isn't well justified, a suspicious absence of any anomaly claim given what's been tested, an anomaly whose evidence fails the inference_validity_check, a previously-raised gap that still hasn't been addressed, or a coverage-breadth problem (see coverage_breadth_check). 'strong_enough' whenever none of those apply - routine, low-stakes untested corners in 'gaps' do NOT by themselves require 'weak'; there's always something more you could test in open-ended exploration, and naming it is not the same as having a material objection. But a coverage-breadth problem IS material: concluding 'no anomaly found' or a general behavior characterization from a small slice of the interface, while whole documented behaviors or paths remain completely untouched, is not adequately supported no matter how clean that small slice looks.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete gaps, untested areas, or weak assumptions - things that, if tested, might change the picture. These are for the next checkpoint's planning; listing them does not by itself imply 'weak'.",
                "minItems": 2,
            },
            "coverage_breadth_check": {
                "type": "string",
                "description": (
                    "Look at gaps/untested_areas as a SET, not one at a time: roughly how many genuinely "
                    "distinct, documented behaviors or paths - not just parameter variations within a path "
                    "that's already been tested - have zero test coverage so far? If that's a large "
                    "fraction of what the interface actually offers, say so explicitly - that breadth gap "
                    "is itself a material reason the overall characterization or a 'no anomaly found' "
                    "conclusion isn't adequately supported yet, independent of how solid the small, "
                    "already-tested slice looks. Testing a handful of easy, narrow checks and concluding "
                    "the system is fine is not the same as testing broadly across the documented surface "
                    "and finding nothing - don't let the former stand in for the latter."
                ),
            },
            # NOTE: a known, accepted limitation lives here - this check does not
            # account for realistic value rounding/precision when deciding whether
            # cited evidence "discriminates" a claim from its rival. Verified case:
            # a claim that 101 credits costing $1.82 instead of $1.818 proves
            # "marginal pricing" is actually just ordinary cent-rounding of a flat
            # rate, but the Skeptic accepted it as discriminating evidence anyway.
            # Deliberately not fixed here - carried forward as documented, known
            # scope, not something to patch incidentally while touching this file.
            "inference_validity_check": {
                "type": "string",
                "description": (
                    "For each anomaly claimed (if any): does the cited evidence actually DISCRIMINATE "
                    "the claimed mechanism from its own stated rival explanation - i.e. would the "
                    "evidence have come out differently if the rival were true instead - or would the "
                    "exact same observations show up under either explanation? Evidence that is merely "
                    "CONSISTENT with a claim (but equally consistent with a real rival) does not actually "
                    "support that claim, no matter how many data points there are. Concretely check: "
                    "restate the claimed mechanism, restate the rival, and ask whether the specific "
                    "numbers/outcomes cited would differ between them. Explicitly name any anomaly that "
                    "fails this test, and say what a genuinely discriminating test would need to show "
                    "instead. If no anomalies were claimed, write 'n/a'."
                ),
            },
            "anomaly_critique": {
                "type": "string",
                "description": (
                    "If any anomalies were claimed: your independent alternative explanation for the same "
                    "observation(s), and whether each claim's own competing explanation was genuine or a "
                    "strawman, plus concrete ways to distinguish rival explanations from the claim. If no "
                    "anomalies were claimed, briefly say whether that absence itself seems premature given "
                    "what's been tested, or whether a genuinely clean result is plausible here."
                ),
            },
            "recommended_next_tests": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "At least 2 concrete, actionable test ideas the Driver should try next - specific "
                    "enough to run directly (what inputs, what outcome would be informative). Prioritize "
                    "tests that would resolve a failed inference_validity_check or close a named gap over "
                    "generic 'test more' suggestions."
                ),
                "minItems": 2,
            },
            "prior_critique_addressed": {
                "type": "string",
                "description": (
                    "Only meaningful if 'your_own_prior_review' is present in the evidence you were given "
                    "(your own critique from the previous checkpoint). Check explicitly, using the Driver's "
                    "'prior_gaps_response' (its own stated status for each gap you named): were the "
                    "gaps/recommended_next_tests you named last time actually acted on with new tests, "
                    "and for anything the Driver instead marked as untestable-with-current-data or "
                    "already-resolved, is that stated reason actually credible - or is it hand-wavy, "
                    "unsupported, or a way to dodge an inconvenient test? A credible untestable/resolved "
                    "claim should NOT count against the hypothesis. An incredible one, or a gap with no "
                    "response at all, is a material reason for 'weak' on its own. If there was no prior "
                    "review, write 'n/a'."
                ),
            },
            "reasoning": {"type": "string"},
        },
        "required": [
            "verdict", "gaps", "coverage_breadth_check", "inference_validity_check", "anomaly_critique",
            "recommended_next_tests", "prior_critique_addressed", "reasoning",
        ],
    },
}

SKEPTIC_SYSTEM_PROMPT = """You are cold-reviewing a checkpoint hypothesis - you have NOT seen the raw
test data, only the hypothesis itself (its behavior characterization and any anomaly claims). Your
job is to poke holes, not confirm.

Beyond "is there enough evidence," check whether the evidence is the RIGHT KIND of evidence - this is
a distinct failure mode from insufficient evidence, and it's easy to miss. A claim can cite several
real, correctly-observed data points and still be unsupported, if those same data points would have
looked identical under a rival explanation. Evidence only supports a claim over its rival if it would
have come out DIFFERENTLY had the rival been true instead - evidence that's merely consistent with
(but doesn't rule out) an alternative is not actually evidence for the claim, regardless of volume.

For example: if a claim is "capacity resets per-transaction, not cumulatively" and the cited evidence
is "a large purchase was declined, then smaller purchases after it were approved" - check whether that
observation would look any different under the rival "capacity is cumulative, and the smaller purchases
simply fit within whatever headroom remained." If the numbers involved (the decline amount, the prior
spend, the smaller amounts) are consistent with the cumulative story too, the cited evidence does not
actually discriminate between the two, and the claim is unsupported regardless of how confidently it's
stated. This is exactly the kind of thing inference_validity_check exists to catch - work through it
explicitly rather than treating "some evidence exists" as sufficient.

Do not conflate "I can name an untested corner" with "I have a material objection." Exploratory testing
always has more you could try - that's what 'gaps' and 'recommended_next_tests' are for, feeding the
next checkpoint's planning - but naming them is not itself a reason for "weak". Reserve "weak" for a
genuine, specific reason to doubt the current hypothesis or a named anomaly claim: an inference_validity_check
failure, an overconfident characterization the evidence doesn't support, a suspicious absence of any
anomaly claim given what's actually been tested, a coverage-breadth problem (see below), or (see further
below) a previously-raised objection that was never addressed. If the strongest thing you can say is
"there's always more to test," that is consistent with "strong_enough", not evidence against it.

But do not confuse "a few narrow untested corners" with "most of the documented interface has never
been exercised" - these look similar in isolation but are not the same thing, and only the second is
material on its own. Look at gaps/untested_areas as a SET: if they name genuinely distinct documented
behaviors or paths - not just parameter variations within a path that's already been tested - and that
set covers a large fraction of what the interface actually offers, a "no anomaly found, everything
looks clean" conclusion is not adequately supported, no matter how solid the small tested slice is. For
example: five tests that each cleanly confirm one narrow, easy error path (wrong credential, invalid
quantity, wrong secret) plus one single data point about pricing is not "a well-tested system with a
couple of loose ends" - it's a small, easy fraction of the interface with almost everything else,
including the paths most likely to hide a real bug, never touched even once. Say this explicitly in
coverage_breadth_check and let it drive the verdict; don't let "the tested claims all held up" quietly
stand in for "the interface has actually been tested."

If the evidence you're given includes 'your_own_prior_review' (your own critique from the checkpoint
before this one), check continuity: did the new hypothesis actually respond to what you flagged last
time, or does it just repeat the same kind of evidence in a different direction while ignoring your
critique? The Driver's 'prior_gaps_response' gives its own stated status for each gap you named - don't
just take it at face value. If it claims something is untestable with the current scenario data or
already conclusively resolved, judge whether that specific stated reason actually holds up (e.g. "no
known account has two cards" is a real, checkable reason; "didn't get to it" or a vague gesture is not).
A credible claim closes that gap fairly - don't keep penalizing something that genuinely cannot be
tested further. An incredible claim, or silence on a gap you named, is itself a material reason for
"weak", independent of anything else.

Give a verdict: "weak" only for one of the material reasons above, or "strong_enough" if none apply.
Identify at least 2 concrete gaps, do the coverage_breadth_check honestly, and give at least 2 concrete
recommended_next_tests specific enough to run directly. If anomalies were claimed, give your own
independent alternative explanation and assess whether each one's own competing explanation is genuine
or a strawman - you propose what's worth investigating further, the Driver decides what to actually
test. Remember this implementation may genuinely have no bugs - don't manufacture doubt just to have
something to say, but don't rubber-stamp a thin absence-of-anomalies claim either, and don't rubber-stamp
a thin slice of the interface as if it were the whole thing.

Call submit_skeptic_review with your answer."""


def validate_skeptic_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    required = (
        "verdict", "gaps", "coverage_breadth_check", "inference_validity_check", "anomaly_critique",
        "recommended_next_tests", "prior_critique_addressed", "reasoning",
    )
    for key in required:
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if data.get("verdict") not in ("weak", "strong_enough"):
        errors.append("'verdict' must be 'weak' or 'strong_enough'")
    gaps = data.get("gaps")
    if not isinstance(gaps, list) or len(gaps) < 2 or not all(isinstance(g, str) for g in gaps):
        errors.append("'gaps' must be a list of at least 2 strings")
    next_tests = data.get("recommended_next_tests")
    if not isinstance(next_tests, list) or len(next_tests) < 2 or not all(isinstance(t, str) for t in next_tests):
        errors.append("'recommended_next_tests' must be a list of at least 2 strings")
    return errors


BUG_REPORT_TOOL = {
    "name": "submit_bug_reports",
    "description": "Write the final bug report(s) - deliberately NOT redacted, since this needs real repro steps. One entry per distinct anomaly.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bugs": {
                "type": "array",
                "description": "One entry per distinct anomaly in the final hypothesis.",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "steps_to_reproduce": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                        "expected_behavior": {"type": "string"},
                        "actual_behavior": {"type": "string"},
                        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                        "status": {
                            "type": "string",
                            "enum": ["corroborated", "inconclusive"],
                            "description": "'corroborated' if Skeptic was satisfied; 'inconclusive' if the checkpoint budget ran out while Skeptic still had objections.",
                        },
                        "caveats": {"type": "string", "description": "Honest caveats about what wasn't resolved or verified."},
                    },
                    "required": ["title", "description", "steps_to_reproduce", "expected_behavior", "actual_behavior", "severity", "status", "caveats"],
                },
            },
        },
        "required": ["bugs"],
    },
}

BUG_REPORT_SYSTEM_PROMPT = """Write the final bug report(s) based on everything in the evidence: the
final checkpoint hypothesis (including its anomaly claims), Skeptic's critique, stopped_reason, and
the full test history. Write ONE entry per distinct anomaly claimed in the final hypothesis. Include
literal, concrete repro steps (real values that actually reproduced the issue, referencing real test
numbers) - each report needs to be independently actionable, not a redacted summary. Be honest in
caveats about anything that wasn't fully resolved - if the checkpoint budget ran out while Skeptic
still had objections (stopped_reason is "checkpoints_exhausted"), say so explicitly rather than
overstating confidence, and set status to "inconclusive" rather than "corroborated".

Call submit_bug_reports with your answer."""


def validate_bug_reports(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    bugs = data.get("bugs")
    if not isinstance(bugs, list) or not bugs:
        errors.append("'bugs' must be a non-empty list")
        return errors
    for i, bug in enumerate(bugs):
        if not isinstance(bug, dict):
            errors.append(f"bugs[{i}] must be an object")
            continue
        for key in ("title", "description", "steps_to_reproduce", "expected_behavior", "actual_behavior", "severity", "status", "caveats"):
            if key not in bug:
                errors.append(f"bugs[{i}] missing '{key}'")
        steps = bug.get("steps_to_reproduce")
        if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
            errors.append(f"bugs[{i}].steps_to_reproduce must be a non-empty list of strings")
        if bug.get("severity") not in ("low", "medium", "high"):
            errors.append(f"bugs[{i}].severity must be low/medium/high")
        if bug.get("status") not in ("corroborated", "inconclusive"):
            errors.append(f"bugs[{i}].status must be 'corroborated' or 'inconclusive'")
    return errors
