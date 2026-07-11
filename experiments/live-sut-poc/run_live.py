"""
Live version of pattern-detection-poc, with a blind casting phase in front of it:
instead of handing the investigator a call log that already contains the anomaly, it
only sees 8 normal baseline calls (start sut.py with `uvicorn sut:app --port 8000`
first) and has to propose its own batch of candidate hypotheses (each with test
ideas) plus pure edge-case probes against the live SUT, with no hint that any bug
exists or what it might look like. The real vulnerability stays exactly as-is in
sut.py, undisclosed.

Casting is broken into small checkpoints. If a checkpoint's rounds find nothing
anomalous, the Driver is forced to characterize the SUT's behavior so far (not "what's
the bug" - there may not be one yet - but "what have I learned, what's still
untested"), and a cold Skeptic pass pokes holes in that characterization before the
next checkpoint's rounds proceed, informed by the critique. This guarantees Skeptic
gets exercised even on a run that never finds anything, rather than sitting completely
unused (which is what happened on every prior run before this).

Only if a test comes back genuinely anomalous does the flow continue into formal
hypothesis formation, a separate cold Skeptic review of that specific claim, real
confirm/disconfirm test execution, and a bounded follow-up loop that also tries to
operationalize Skeptic's own disproof strategies - this part is unchanged from before.
"""

import itertools
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from report import render_report

# Model-generated text (reasoning, probes) can contain non-ASCII characters (e.g. "~=")
# that the default Windows console codec can't encode, crashing a plain print().
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL = "claude-sonnet-4-6"
MAX_ATTEMPTS = 3
SUT_URL = "http://localhost:8000/analyze"
SUT_DOCS_URL = "http://localhost:8000/docs"
SUT_READY_TIMEOUT = 5.0

# sut.py's regex (^([a-zA-Z]+)+$) backtracks catastrophically on any run of letters -
# same letter repeated, mixed letters, either case, it doesn't matter, only the run
# length does - immediately followed by one non-letter character. The known-safe
# threshold below applies to ANY test text this harness executes, whether proposed
# during casting, confirm/disconfirm, or follow-up - a proposed test with a longer
# alphabetic run than this is untested and could hang the SUT for a very long time,
# so it's refused rather than executed.
MAX_SAFE_ALPHA_RUN = 25

# A flat latency threshold can't tell "genuinely pathological" apart from "long but
# ordinary input" (the SUT's normal cost model is linear in length). Instead, "slow"
# means measured latency is SLOW_MULTIPLIER-x what the empirical baseline rate (from
# the real, just-measured baseline calls) would predict for that input's length, with
# SLOW_THRESHOLD_MS as a floor so short inputs aren't flagged on noise.
#
# 500 was too conservative: a real run hit a genuine escalating anomaly (65ms at one
# length, 184ms one letter longer, 339ms one more after that - a clear 5-10x elevation
# over the linear baseline) and every one of those stayed under the 500ms floor, so
# "slow" never fired even though the Driver's own reasoning had already recognized the
# pattern. Every observed genuinely-normal short input has stayed under ~110ms, so 150
# keeps a healthy margin over real noise while letting a 5x+ elevation actually cross.
SLOW_THRESHOLD_MS = 150
SLOW_MULTIPLIER = 5

ROUNDS_PER_CHECKPOINT = 2
MAX_CHECKPOINTS = 2
MAX_FOLLOWUP_ROUNDS = 2

# The very first round is the only genuinely "know nothing" moment - nothing has
# been tested or ruled out yet, so it's the right place to spend extra breadth.
# Every later round already has real results to narrow against, so stays at the
# default budget.
FIRST_ROUND_TEST_BUDGET = 10
DEFAULT_TEST_BUDGET = 6

BASELINE_TEXTS = [
    "Hello there, testing.",
    "A short one.",
    "This is a moderately long input string for testing.",
    "Short again.",
    "Here is a somewhat longer message than before, used to check timing.",
    "Tiny.",
    "This one is quite a bit longer than most of the previous test messages we've sent so far.",
    "Medium length input for the ninth or so call in this sequence.",
]

# Matches a run of 16+ letters (any mix, either case - not just repeated identical
# ones, since the vulnerability no longer requires that), optionally followed by one
# non-letter character (the pathological pattern's structure: a long alphabetic run +
# a terminator that breaks it). 16 is comfortably below the real ~23-char onset, so
# this also catches "close but not quite" probe attempts, while staying well above
# ordinary long English words (rare past ~12-14 letters) to avoid over-redacting
# normal prose.
_ALPHA_RUN_PATTERN = re.compile(r"[a-zA-Z]{16,}[^a-zA-Z]?")


def redact_pathological_content(text: str) -> str:
    """Replace literal long-alphabetic-run content with a structural placeholder
    (length only, no character identity) before handing text to any LLM call other
    than the one that actually has to compose real, executable request bodies. This
    is a deliberate blind spot: "aaaa...a!" is a famous textbook ReDoS trigger, and a
    model that sees it verbatim might be recognizing a shape from training data
    rather than inferring cause from the timing signal alone. Keeping the length
    visible while hiding the actual characters used still lets genuine structural
    reasoning happen.
    """
    if not isinstance(text, str):
        return text

    def replace_run(match):
        full = match.group(0)
        has_terminator = bool(full) and not full[-1].isalpha()
        run_len = len(full) - (1 if has_terminator else 0)
        if has_terminator:
            return f"[a run of {run_len} letters, followed by one non-letter character]"
        return f"[a run of {run_len} letters]"

    return _ALPHA_RUN_PATTERN.sub(replace_run, text)


def redact_calls_for_model(calls: list[dict]) -> list[dict]:
    """Deep-copy calls with request text redacted - never mutates the original list,
    which is still written to results/output.json with full fidelity for humans."""
    redacted = json.loads(json.dumps(calls))
    for call in redacted:
        call["request"]["body"]["text"] = redact_pathological_content(call["request"]["body"]["text"])
    return redacted


def redact_history_for_model(history: list[dict]) -> list[dict]:
    """Same idea as redact_calls_for_model, for the round-by-round history shown to
    get_followup/get_casting_round: redact the literal executed text and any
    free-text fields that might quote it, without touching the original (kept intact
    for output.json)."""
    redacted = json.loads(json.dumps(history))
    for entry in redacted:
        if "request" in entry:
            entry["request"]["body"]["text"] = redact_pathological_content(entry["request"]["body"]["text"])
        for key in ("predicted_outcome", "skip_reason", "round_reasoning", "linked_hypothesis"):
            if key in entry:
                entry[key] = redact_pathological_content(entry[key])
    return redacted


def call_tool_with_retry(client, *, model, system, tools, tool_name, user_message, validate_fn, max_tokens):
    """Shared call->validate->retry loop used by every tool-forced Claude call in this file.

    Retries are informed, not blind repeats: on failure, the model's own malformed call and
    the concrete validation errors are fed back as a tool_result before asking again, so a
    systematic misunderstanding (e.g. an omitted required field) has a chance to self-correct
    instead of reproducing the identical mistake on every attempt.
    """
    messages = [{"role": "user", "content": user_message}]
    last_errors = ["no attempts made"]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )

        tool_use = next((block for block in message.content if block.type == "tool_use"), None)
        if tool_use is None:
            last_errors = [f"no tool_use block in response (stop_reason={message.stop_reason})"]
            print(f"  attempt {attempt} produced no tool call: {last_errors} - retrying")
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user", "content": "You must call the tool. Try again."})
            continue

        errors = validate_fn(tool_use.input)
        if not errors:
            return tool_use.input

        last_errors = errors
        print(f"  attempt {attempt} produced malformed output: {errors} - retrying")
        messages.append({"role": "assistant", "content": message.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": "Invalid: " + "; ".join(errors) + ". Fix and call the tool again with a corrected, complete answer.",
                "is_error": True,
            }],
        })

    raise RuntimeError(f"Gave up after {MAX_ATTEMPTS} attempts, last errors: {last_errors}")


def call_sut(client: httpx.Client, text: str) -> dict:
    start = time.perf_counter()
    response = client.post(SUT_URL, json={"text": text}, timeout=60.0)
    measured_latency_ms = (time.perf_counter() - start) * 1000
    return {
        "status": response.status_code,
        "body": response.json(),
        "measured_latency_ms": round(measured_latency_ms, 1),
    }


def build_call_log(client: httpx.Client) -> list[dict]:
    """Only the 8 normal baseline calls - no pre-baked anomaly. Whatever the Driver
    ends up finding (if anything) has to come from its own casting."""
    calls = []
    for i, text in enumerate(BASELINE_TEXTS, start=1):
        print(f"  calling baseline {i}...")
        result = call_sut(client, text)
        calls.append(
            {
                "index": i,
                "request": {"method": "POST", "path": "/analyze", "body": {"text": text}},
                "response": result,
            }
        )
    return calls


def fit_baseline_latency_model(calls: list[dict]) -> tuple[float, float]:
    """Least-squares fit of measured latency to text length: latency_ms = intercept + slope * length.

    A per-call measured/length ratio (the previous approach) conflates the fixed per-request
    overhead with the true marginal per-char cost - for a short baseline text, the ~8ms fixed
    overhead dominates that ratio, inflating the apparent "rate" well above the real marginal
    slope. That inflated rate then overestimates expected latency most for short inputs -
    exactly where this SUT's real anomaly lives, and exactly where under-sensitivity matters
    most. A real regression separates the fixed intercept from the marginal slope instead.
    """
    lengths = [len(c["request"]["body"]["text"]) for c in calls]
    latencies = [c["response"]["measured_latency_ms"] for c in calls]
    n = len(calls)
    sum_x = sum(lengths)
    sum_y = sum(latencies)
    sum_xy = sum(x * y for x, y in zip(lengths, latencies))
    sum_x2 = sum(x * x for x in lengths)
    slope = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)
    intercept = (sum_y - slope * sum_x) / n
    return intercept, slope


def longest_alpha_run(text: str) -> int:
    """Longest run of CONSECUTIVE letters (any mix, either case) - the dangerous
    quantity for sut.py's vulnerability, which doesn't care which letters or whether
    they repeat, only how long the unbroken alphabetic stretch is."""
    return max((len(list(group)) for is_alpha, group in itertools.groupby(text, key=str.isalpha) if is_alpha), default=0)


def classify_latency(measured_ms: float, text_length: int, baseline_model: tuple[float, float]) -> str:
    intercept_ms, slope_ms_per_char = baseline_model
    expected_ms = intercept_ms + slope_ms_per_char * text_length
    threshold = max(SLOW_THRESHOLD_MS, expected_ms * SLOW_MULTIPLIER)
    return "slow" if measured_ms > threshold else "fast"


def execute_test(client: httpx.Client, test: dict, baseline_model: tuple[float, float]) -> dict:
    text = test["text"]
    request = {"method": "POST", "path": "/analyze", "body": {"text": text}}
    run_length = longest_alpha_run(text)
    if run_length > MAX_SAFE_ALPHA_RUN:
        print(
            f"  refusing to execute: longest alphabetic run is {run_length}, "
            f"exceeds safe ceiling of {MAX_SAFE_ALPHA_RUN}"
        )
        return {
            "request": request,
            "predicted_outcome": test["predicted_outcome"],
            "predicted_latency_class": test["predicted_latency_class"],
            "skipped": True,
            "skip_reason": (
                f"longest alphabetic run is {run_length} chars, exceeding the "
                f"calibrated safe ceiling of {MAX_SAFE_ALPHA_RUN} - refused to avoid "
                "hanging the SUT for an unknown, potentially very long time."
            ),
        }

    # Re-warm the connection before timing: an idle gap (e.g. the Claude API call, or a
    # skipped confirm_test never firing a request) makes the next request on this same
    # client pay a ~2s reconnection cost that has nothing to do with the SUT's own
    # latency - discovered when a disconfirm_test with no repeated characters at all
    # measured 2s despite the SUT reporting 38ms of actual work.
    client.get(SUT_DOCS_URL, timeout=SUT_READY_TIMEOUT)

    result = call_sut(client, text)
    actual_latency_class = classify_latency(result["measured_latency_ms"], len(text), baseline_model)
    return {
        "request": request,
        "response": result,
        "predicted_outcome": test["predicted_outcome"],
        "predicted_latency_class": test["predicted_latency_class"],
        "actual_measured_latency_ms": result["measured_latency_ms"],
        "actual_latency_class": actual_latency_class,
        "prediction_matched": actual_latency_class == test["predicted_latency_class"],
    }


def unwrap_accidental_json_body(text: str) -> str:
    """Defends against the model wrapping its probe in the request envelope itself
    (e.g. '{"text": "..."}') instead of providing raw text content - seen in practice
    despite an explicit instruction not to, so the prompt fix alone isn't trusted."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            return parsed["text"]
    return text


CASTING_TOOL = {
    "name": "submit_casting_round",
    "description": (
        "Propose a batch of tests: some testing specific candidate hypotheses about "
        "possible bugs, and some pure edge-case probes not tied to any theory - or "
        "report that you have nothing more worth trying."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "give_up": {
                "type": "boolean",
                "description": "True if you believe you've explored reasonably and have nothing more worth proposing.",
            },
            "reasoning": {
                "type": "string",
                "description": "Overall reasoning for this round's set of tests (or for giving up).",
            },
            "candidate_tests": {
                "type": "array",
                "description": (
                    "See the system prompt for how many tests to propose this round. Each is EITHER "
                    "tied to a specific candidate hypothesis (set linked_hypothesis to that theory, "
                    "stated in full) OR a pure edge-case/negative-case probe not tied to any theory "
                    "(set linked_hypothesis to an empty string). Mix both kinds in the same list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "linked_hypothesis": {
                            "type": "string",
                            "description": "The specific candidate theory this test checks, stated in full. Empty string \"\" if this is a pure edge-case probe.",
                        },
                        "text": {"type": "string", "description": "The literal text to send as the request body."},
                        "predicted_outcome": {"type": "string", "description": "Free-text prediction of what will happen."},
                        "predicted_latency_class": {"type": "string", "enum": ["fast", "slow"]},
                    },
                    "required": ["linked_hypothesis", "text", "predicted_outcome", "predicted_latency_class"],
                },
            },
        },
        "required": ["give_up", "reasoning", "candidate_tests"],
    },
}

def casting_system_prompt(test_budget: int, is_first_round: bool) -> str:
    if is_first_round:
        context_instruction = """Before proposing anything, think about context: what can you reasonably assume
about this kind of system (a text-scoring endpoint) given its apparent purpose, and what bug classes are
commonly seen in this category of implementation (e.g. encoding/counting mismatches, boundary and length
handling, regex-based validation, resource exhaustion on adversarial input, caching/statefulness bugs)? Use
that to inform your hypotheses - not as a substitute for testing, but as a reason to prioritize some
categories over others when you're starting from nothing. State this reasoning explicitly."""
    else:
        context_instruction = """You now have real test results, not just assumptions about this kind of system.
Before proposing anything, briefly state what you've actually learned so far (not what's typical for this
category in general, but what THIS system has actually shown) and how that's changing your approach this
round - narrowing toward what looks promising, or ruling out categories that turned out unremarkable."""

    return f"""You are testing a live API endpoint (POST /analyze, a text-scoring
service) to look for bugs or unexpected behavior. You've been shown baseline calls establishing
normal behavior - latency scales predictably with input length. Nothing is currently flagged as
anomalous, and you do not know whether any bug exists at all.

{context_instruction}

In one round, propose a BATCH of tests - up to {test_budget} total:
1. Candidate hypotheses: think of a few specific, falsifiable theories about possible bugs (e.g.
   related to input length, character content, encoding, whitespace, or anything else a careful
   tester would suspect). For each, propose 1-2 concrete test ideas designed to check it - a literal
   request body and a prediction of what would happen if that specific theory were true. Set
   linked_hypothesis to the full theory text for these.
2. Pure edge-case probes: also propose some tests not tied to any specific theory - just general
   negative-case/boundary testing instinct (empty input, unusual characters, whitespace, very long
   input, etc). For these, predict "fast" (the null hypothesis: expect normal behavior) and set
   linked_hypothesis to an empty string.

All of these tests will be executed for real, together, before you see any results - they don't
depend on each other's outcomes, so make each one a genuinely independent check rather than a
refinement of another test in the same batch. You'll see every real result before being asked for
another round, and can refine across rounds then.

If an earlier round's test was refused rather than executed (check for a skip_reason in
tests_tried_in_earlier_rounds) and you still think that specific hypothesis is worth pursuing, try a
substantially reduced/shorter version of the same idea in this round before moving on to a different
hypothesis. A refusal doesn't mean the theory is wrong - it means that specific attempt was too
extreme to safely run. Don't abandon a promising theory just because one attempt at it got refused.

If you're testing a hypothesis about catastrophic regex backtracking (ReDoS): a repeated or patterned
string that ends up matching a pattern cleanly is fast to process - there's no ambiguity for a regex
engine to backtrack over, so no slowdown occurs. Catastrophic slowdown specifically requires the
overall match to fail after a long run of ambiguity - something has to break the pattern. If a test
of this kind came back fast, consider whether it actually included something that would cause a match
failure, not just a longer version of a string that matches cleanly. For this class of vulnerability,
which specific character breaks the match generally doesn't matter - what matters is that one does.

If you believe you've explored reasonably and have no more good ideas worth proposing, set give_up
to true rather than proposing something arbitrary just to have something to submit.

If "prior_checkpoint_feedback" is present in the evidence, it's an independent cold critique of a
behavior characterization from your last checkpoint - including gaps and untested areas it flagged.
Use it to inform what you prioritize this round, though you still decide what to actually test, not
the critique itself.

Prioritize breadth over depth. Before proposing a test, check tests_tried_in_earlier_rounds: if the
same underlying question has already been asked multiple times with consistent results (e.g.,
several length tests all confirming the same linear scaling), treat it as settled - don't ask a
third or fourth variant of it unless something specific suggests the picture has actually changed.
If an earlier test's own reasoning admitted it couldn't really test what it intended (for example,
because your only lever here is the literal text content, not the request's HTTP method, headers,
or structure), do not propose that same non-test again - either find a genuinely different way to
probe the idea using text content alone, or abandon it for a different category entirely. When
prior_checkpoint_feedback names gaps, prioritize genuinely new categories of text content over
refining a theory that already has consistent supporting evidence.

Call submit_casting_round with your answer."""


def validate_casting_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("give_up", "reasoning", "candidate_tests"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if not isinstance(data.get("give_up"), bool):
        errors.append("'give_up' must be a boolean")

    tests = data.get("candidate_tests")
    if not isinstance(tests, list):
        errors.append("'candidate_tests' must be a list")
        return errors

    if not data.get("give_up") and len(tests) == 0:
        errors.append("'candidate_tests' must be non-empty when give_up is false")

    for i, test in enumerate(tests):
        if not isinstance(test, dict):
            errors.append(f"candidate_tests[{i}] must be an object")
            continue
        if not isinstance(test.get("linked_hypothesis"), str):
            errors.append(f"candidate_tests[{i}].linked_hypothesis must be a string")
        if not isinstance(test.get("text"), str):
            errors.append(f"candidate_tests[{i}].text must be a string")
        if test.get("predicted_latency_class") not in ("fast", "slow"):
            errors.append(f"candidate_tests[{i}].predicted_latency_class must be 'fast' or 'slow'")
        if "predicted_outcome" not in test:
            errors.append(f"candidate_tests[{i}] missing required field 'predicted_outcome'")

    return errors


def get_casting_round(
    client: Anthropic,
    calls: list[dict],
    casting_log: list[dict],
    prior_checkpoint_feedback: dict | None = None,
    *,
    test_budget: int = 6,
    is_first_round: bool = False,
) -> dict:
    evidence = {
        "scenario": f"{len(calls)} established baseline calls to POST /analyze, a text-scoring endpoint. Same endpoint, same session.",
        "baseline_calls": redact_calls_for_model(calls),
        "tests_tried_in_earlier_rounds": redact_history_for_model(casting_log),
    }
    if prior_checkpoint_feedback is not None:
        evidence["prior_checkpoint_feedback"] = prior_checkpoint_feedback
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=casting_system_prompt(test_budget, is_first_round),
        tools=[CASTING_TOOL],
        tool_name="submit_casting_round",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_casting_response,
        max_tokens=3072 if test_budget <= 6 else 5120,
    )


BEHAVIOR_HYPOTHESIS_TOOL = {
    "name": "submit_behavior_hypothesis",
    "description": "Characterize the SUT's behavior based on everything tested so far, whether or not anything anomalous was found.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_behavior": {
                "type": "string",
                "description": "A clear, well-balanced characterization of what's been learned: confirmed patterns, the latency model, categories tested and found normal (or not) - not a guess about a specific bug.",
            },
            "untested_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 1-2 categories or ideas not yet tried that are worth testing next.",
                "minItems": 1,
            },
        },
        "required": ["observed_behavior", "untested_areas"],
    },
}

BEHAVIOR_HYPOTHESIS_SYSTEM_PROMPT = """You have been testing a live API endpoint across one or more
rounds. Characterize the SUT's actual behavior based on everything tested so far - a clear,
well-balanced description of what you've learned, not a guess about a specific bug (there may not
be one). Be honest about what's actually been verified versus what's still assumed. Also list at
least 1-2 categories of input or behavior that haven't been tried yet and would be worth testing
next.

Call submit_behavior_hypothesis with your answer."""


def validate_behavior_hypothesis(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("observed_behavior", "untested_areas"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    areas = data.get("untested_areas")
    if not isinstance(areas, list) or len(areas) < 1 or not all(isinstance(a, str) for a in areas):
        errors.append("'untested_areas' must be a non-empty list of strings")

    return errors


def get_behavior_hypothesis(client: Anthropic, calls: list[dict], casting_log: list[dict]) -> dict:
    evidence = {
        "baseline_calls": redact_calls_for_model(calls),
        "tests_tried_so_far": redact_history_for_model(casting_log),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=BEHAVIOR_HYPOTHESIS_SYSTEM_PROMPT,
        tools=[BEHAVIOR_HYPOTHESIS_TOOL],
        tool_name="submit_behavior_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_behavior_hypothesis,
        max_tokens=1536,
    )


BEHAVIOR_SKEPTIC_TOOL = {
    "name": "submit_behavior_skeptic_review",
    "description": "Cold-review a behavior characterization - you have NOT seen the underlying test data. Poke holes in it; don't rubber-stamp it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessment": {
                "type": "string",
                "enum": ["well_supported", "premature"],
                "description": "Is this characterization adequately supported by what it claims to have tested, or overconfident given known gaps?",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete gaps, untested areas, or weak assumptions in this characterization - things that, if tested, might change the picture.",
                "minItems": 2,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["assessment", "gaps", "reasoning"],
    },
}

BEHAVIOR_SKEPTIC_SYSTEM_PROMPT = """You are reviewing a characterization of a system's behavior,
written by another investigator based on testing so far. You have NOT seen the raw test data - only
the characterization itself (its description of observed behavior and what it says is untested).
Your job is to poke holes in it, not confirm it.

Assess whether this characterization is well-supported by what it claims to have tested, or
premature/overconfident given the gaps. Identify at least 2 concrete gaps, untested areas, or weak
assumptions. You are proposing what's worth investigating, not designing the actual tests yourself -
a separate step (the Driver) decides what to test next, informed by your critique.

Call submit_behavior_skeptic_review with your answer."""


def validate_behavior_skeptic_review(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("assessment", "gaps", "reasoning"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if data.get("assessment") not in ("well_supported", "premature"):
        errors.append("'assessment' must be 'well_supported' or 'premature'")

    gaps = data.get("gaps")
    if not isinstance(gaps, list) or len(gaps) < 2 or not all(isinstance(g, str) for g in gaps):
        errors.append("'gaps' must be a list of at least 2 strings")

    return errors


def get_behavior_skeptic_review(client: Anthropic, behavior_hypothesis: dict) -> dict:
    evidence = {
        "observed_behavior": redact_pathological_content(behavior_hypothesis["observed_behavior"]),
        "untested_areas": [redact_pathological_content(a) for a in behavior_hypothesis["untested_areas"]],
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=BEHAVIOR_SKEPTIC_SYSTEM_PROMPT,
        tools=[BEHAVIOR_SKEPTIC_TOOL],
        tool_name="submit_behavior_skeptic_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_behavior_skeptic_review,
        max_tokens=1536,
    )


def run_checkpoint_cycle(anthropic_client: Anthropic, http_client: httpx.Client, calls: list[dict], baseline_model: tuple[float, float]):
    """Casting broken into small checkpoints. Each checkpoint runs up to
    ROUNDS_PER_CHECKPOINT rounds (same batch-propose-and-execute mechanism as before,
    same early-exit the moment a test comes back slower than the empirical baseline
    predicts). If a checkpoint's rounds find nothing, the Driver is forced to
    characterize the SUT's behavior so far and Skeptic critiques it - guaranteeing
    Skeptic gets exercised even when discovery fails outright - and that critique
    feeds into the next checkpoint's rounds.

    Returns (casting_log, anomaly_entry, gave_up, behavior_checkpoints).
    anomaly_entry is None if nothing anomalous was found after all checkpoints.
    casting_log is a flat list of every executed test result across every
    checkpoint/round. behavior_checkpoints is a list of {checkpoint,
    behavior_hypothesis, behavior_skeptic_review} - one entry per checkpoint whose
    rounds found nothing (skipped entirely for a checkpoint that did find something,
    since that goes straight to the richer Phase B output instead).
    """
    casting_log = []
    behavior_checkpoints = []
    prior_feedback = None

    for checkpoint_num in range(1, MAX_CHECKPOINTS + 1):
        anomaly_entry = None
        gave_up_this_checkpoint = False

        for round_num in range(1, ROUNDS_PER_CHECKPOINT + 1):
            is_very_first_round = checkpoint_num == 1 and round_num == 1
            test_budget = FIRST_ROUND_TEST_BUDGET if is_very_first_round else DEFAULT_TEST_BUDGET
            print(f"Asking Claude for a casting round (checkpoint {checkpoint_num}, round {round_num}, budget {test_budget})...")
            casting = get_casting_round(
                anthropic_client,
                calls,
                casting_log,
                prior_feedback,
                test_budget=test_budget,
                is_first_round=is_very_first_round,
            )
            if casting["give_up"]:
                print(f"  Claude gave up casting: {casting['reasoning']}")
                gave_up_this_checkpoint = True
                break

            print(f"  round reasoning: {casting['reasoning']}")
            for test in casting["candidate_tests"]:
                text = unwrap_accidental_json_body(test["text"])
                linked = test["linked_hypothesis"]
                label = f"hypothesis: {linked}" if linked else "edge case"
                print(f"  test ({label}): {text!r}")

                synthetic_test = {
                    "text": text,
                    "predicted_outcome": test["predicted_outcome"],
                    "predicted_latency_class": test["predicted_latency_class"],
                }
                result = execute_test(http_client, synthetic_test, baseline_model)
                entry = {
                    "checkpoint": checkpoint_num,
                    "round": round_num,
                    "round_reasoning": casting["reasoning"],
                    "linked_hypothesis": linked,
                    **result,
                }
                casting_log.append(entry)

                if result.get("skipped"):
                    print("    refused as unsafe - no signal, continuing")
                    continue

                print(f"    measured {result['actual_measured_latency_ms']}ms - {result['actual_latency_class']}")
                if result["actual_latency_class"] == "slow" and anomaly_entry is None:
                    print(f"    anomaly found ({label})")
                    anomaly_entry = entry

            if anomaly_entry is not None:
                break

        if anomaly_entry is not None:
            return casting_log, anomaly_entry, False, behavior_checkpoints

        # This checkpoint's rounds ended with nothing found (either exhausted the
        # round budget, or the Driver gave up) - always characterize behavior and
        # get it critiqued, so a run never ends without at least one real Skeptic
        # pass and a final characterization.
        print(f"Checkpoint {checkpoint_num}: nothing found, asking for a behavior hypothesis...")
        behavior_hypothesis = get_behavior_hypothesis(anthropic_client, calls, casting_log)
        print(f"  observed_behavior: {behavior_hypothesis['observed_behavior']}")

        print("Asking behavior-Skeptic for a cold review...")
        behavior_skeptic_review = get_behavior_skeptic_review(anthropic_client, behavior_hypothesis)
        print(f"  assessment: {behavior_skeptic_review['assessment']}")

        behavior_checkpoints.append(
            {
                "checkpoint": checkpoint_num,
                "behavior_hypothesis": behavior_hypothesis,
                "behavior_skeptic_review": behavior_skeptic_review,
            }
        )

        if gave_up_this_checkpoint:
            # The Driver explicitly said it has nothing more to try - stop the whole
            # cycle here; this checkpoint's behavior hypothesis is final.
            return casting_log, None, True, behavior_checkpoints

        prior_feedback = {
            "observed_behavior": redact_pathological_content(behavior_hypothesis["observed_behavior"]),
            "untested_areas": [redact_pathological_content(a) for a in behavior_hypothesis["untested_areas"]],
            "skeptic_assessment": behavior_skeptic_review["assessment"],
            "skeptic_gaps": [redact_pathological_content(g) for g in behavior_skeptic_review["gaps"]],
        }

    return casting_log, None, False, behavior_checkpoints


PATTERN_TOOL = {
    "name": "submit_pattern_hypothesis",
    "description": (
        "Submit the pattern you noticed across the call sequence, which call breaks "
        "it, a hypothesis for why, a competing explanation, and a confirm/disconfirm "
        "test pair designed to discriminate between them - as directly executable "
        "requests, not descriptions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_pattern": {
                "type": "string",
                "description": "The general trend noticed across the sequence of calls.",
            },
            "anomalous_call_index": {
                "type": "integer",
                "description": "The index of the call that breaks the observed pattern.",
            },
            "claim": {
                "type": "string",
                "description": "The primary, narrow, falsifiable hypothesis for why that call breaks the pattern.",
            },
            "competing_explanation": {
                "type": "string",
                "description": "The most plausible alternative explanation a careful engineer would also consider.",
            },
            "severity_if_true": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "confirm_test": {
                "type": "object",
                "description": "A request to actually send to POST /analyze (body {\"text\": ...}) that would confirm the claim if true.",
                "properties": {
                    "text": {"type": "string", "description": "The literal text to send as the request body."},
                    "predicted_outcome": {"type": "string", "description": "Free-text prediction of what will happen."},
                    "predicted_latency_class": {"type": "string", "enum": ["fast", "slow"]},
                },
                "required": ["text", "predicted_outcome", "predicted_latency_class"],
            },
            "disconfirm_test": {
                "type": "object",
                "description": "A request to actually send to POST /analyze (body {\"text\": ...}) whose outcome would genuinely surprise you if the claim were true.",
                "properties": {
                    "text": {"type": "string", "description": "The literal text to send as the request body."},
                    "predicted_outcome": {"type": "string", "description": "Free-text prediction of what will happen."},
                    "predicted_latency_class": {"type": "string", "enum": ["fast", "slow"]},
                },
                "required": ["text", "predicted_outcome", "predicted_latency_class"],
            },
            "why_this_discriminates": {
                "type": "string",
                "description": "Explain why the disconfirm test's outcome would differ between the claim and the competing explanation, not just repeat the same check.",
            },
        },
        "required": [
            "observed_pattern",
            "anomalous_call_index",
            "claim",
            "competing_explanation",
            "severity_if_true",
            "confirm_test",
            "disconfirm_test",
            "why_this_discriminates",
        ],
    },
}

SYSTEM_PROMPT = """You are investigating a sequence of API calls made against a system under test (SUT).
Nothing in the data is pre-flagged as anomalous - you are given the raw sequence of requests and
responses and must notice any pattern yourself, and notice if any call breaks that pattern.
You do NOT have access to ground truth - form your best hypothesis using only the evidence given.

Your confirm_test and disconfirm_test will actually be sent as real POST /analyze requests, not
just described - so `text` must be the literal text to send, not a description of one.

Produce:
1. The general pattern you notice across the sequence.
2. Which single call breaks that pattern.
3. The most likely, narrow, falsifiable hypothesis for why that call breaks the pattern - not
   just an extension of the general pattern's explanation.
4. The most plausible competing explanation for the same evidence - a genuine alternative a
   careful engineer would also consider, not a strawman.
5. A confirm test and a disconfirm test, each a literal request body to send and a prediction of
   whether the response will be "fast" (comparable to the baseline calls) or "slow" (a large
   deviation). The disconfirm test must be designed so its outcome would differ depending on which
   explanation is true - not simply repeat the same check.

Call submit_pattern_hypothesis with your answer."""


def validate_pattern_hypothesis(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    required_top = [
        "observed_pattern",
        "anomalous_call_index",
        "claim",
        "competing_explanation",
        "severity_if_true",
        "confirm_test",
        "disconfirm_test",
        "why_this_discriminates",
    ]
    for key in required_top:
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if "anomalous_call_index" in data and not isinstance(data["anomalous_call_index"], int):
        errors.append("'anomalous_call_index' must be an integer")

    for test_key in ("confirm_test", "disconfirm_test"):
        test = data.get(test_key)
        if not isinstance(test, dict):
            errors.append(f"'{test_key}' must be an object, got {type(test).__name__}")
            continue
        if not isinstance(test.get("text"), str):
            errors.append(f"'{test_key}.text' must be a string")
        if test.get("predicted_latency_class") not in ("fast", "slow"):
            errors.append(f"'{test_key}.predicted_latency_class' must be 'fast' or 'slow'")
        if "predicted_outcome" not in test:
            errors.append(f"'{test_key}' missing required field 'predicted_outcome'")

    if data.get("severity_if_true") not in ("high", "medium", "low"):
        errors.append("'severity_if_true' must be one of high/medium/low")

    return errors


def get_hypothesis(client: Anthropic, calls: list[dict]) -> dict:
    evidence = {
        "scenario": f"{len(calls)} sequential calls to POST /analyze, a text-scoring endpoint. Same endpoint, same session, no other traffic in between.",
        "calls": redact_calls_for_model(calls),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=[PATTERN_TOOL],
        tool_name="submit_pattern_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_pattern_hypothesis,
        max_tokens=2048,
    )


SKEPTIC_TOOL = {
    "name": "submit_skeptic_review",
    "description": (
        "Cold-review a claim and its stated competing explanation - you have NOT seen "
        "the underlying evidence or any test results. Argue against the claim; don't "
        "rubber-stamp it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "skeptic_verdict": {
                "type": "string",
                "enum": ["holds_up", "weak"],
                "description": "Does the claim look solid on a cold read, or does it have real problems?",
            },
            "competing_explanation_assessment": {
                "type": "string",
                "enum": ["genuine", "strawman"],
                "description": "Is the stated competing explanation a real contender, or too weak to seriously threaten the claim?",
            },
            "skeptic_alternative": {
                "type": "string",
                "description": "Your OWN best alternative explanation, formed independently - not just restating the given competing explanation.",
            },
            "disproof_strategies": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete, distinct ways one could test to show this claim is WRONG - genuine falsification angles (e.g. a specific comparison or probe that would surprise you if the claim held), not just restated doubts from your reasoning.",
                "minItems": 2,
            },
            "reasoning": {"type": "string"},
        },
        "required": [
            "skeptic_verdict",
            "competing_explanation_assessment",
            "skeptic_alternative",
            "disproof_strategies",
            "reasoning",
        ],
    },
}

SKEPTIC_SYSTEM_PROMPT = """You are reviewing a hypothesis proposed by another investigator about an
anomaly in a system under test. You have NOT seen the raw evidence, the call log, any test designs,
or any test results - only the claim and the investigator's own stated competing explanation, as
text. Your job is to argue against the claim, not confirm it. Investigators are prone to
confirmation-shaped reasoning even when asked to critique themselves - that's why you exist as a
separate, cold review.

Do not just restate or agree with the given competing explanation. Form your own independent
alternative. Assess whether the given competing explanation is a genuine rival explanation or a
strawman that doesn't seriously threaten the claim. Give your honest verdict on whether the claim
holds up to scrutiny on its own terms, absent any evidence either way.

Also propose at least 2 concrete, distinct ways someone could actually test to prove this claim
WRONG - real falsification strategies (e.g. a specific comparison, a specific probe, a specific
boundary to check), not a restatement of your doubts. Think about what a genuinely surprising
result would look like if the claim were false, not just what would make you personally uneasy.
You are proposing strategies, not writing an executable test yourself - a separate step will try to
turn whichever of your ideas is actually testable into a real request.

Call submit_skeptic_review with your answer."""


def validate_skeptic_review(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("skeptic_verdict", "competing_explanation_assessment", "skeptic_alternative", "disproof_strategies", "reasoning"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if data.get("skeptic_verdict") not in ("holds_up", "weak"):
        errors.append("'skeptic_verdict' must be 'holds_up' or 'weak'")

    if data.get("competing_explanation_assessment") not in ("genuine", "strawman"):
        errors.append("'competing_explanation_assessment' must be 'genuine' or 'strawman'")

    strategies = data.get("disproof_strategies")
    if not isinstance(strategies, list) or len(strategies) < 2 or not all(isinstance(s, str) for s in strategies):
        errors.append("'disproof_strategies' must be a list of at least 2 strings")

    return errors


def get_skeptic_review(client: Anthropic, claim: str, competing_explanation: str) -> dict:
    evidence = {
        "claim": redact_pathological_content(claim),
        "competing_explanation": redact_pathological_content(competing_explanation),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SKEPTIC_SYSTEM_PROMPT,
        tools=[SKEPTIC_TOOL],
        tool_name="submit_skeptic_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_skeptic_review,
        max_tokens=1536,
    )


FOLLOWUP_TOOL = {
    "name": "submit_followup",
    "description": (
        "Given the hypothesis and what actually happened when the confirm/disconfirm "
        "tests were run, decide whether the hypothesis is corroborated, refuted, or "
        "inconclusive - and if not corroborated, propose one new test to actually run."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["corroborated", "refuted", "inconclusive"]},
            "reasoning": {"type": "string", "description": "Why this verdict, given what actually happened."},
            "continue_investigation": {
                "type": "boolean",
                "description": "True if there's a next test worth running to sharpen or settle this.",
            },
            "next_test": {
                "type": "object",
                "description": "Required if continue_investigation is true. A new request to actually send.",
                "properties": {
                    "text": {"type": "string", "description": "The literal text to send as the request body."},
                    "predicted_outcome": {"type": "string"},
                    "predicted_latency_class": {"type": "string", "enum": ["fast", "slow"]},
                },
                "required": ["text", "predicted_outcome", "predicted_latency_class"],
            },
        },
        "required": ["verdict", "reasoning", "continue_investigation"],
    },
}

FOLLOWUP_SYSTEM_PROMPT = f"""You are continuing an investigation into an anomaly in a system under test.
You already formed a hypothesis and a competing explanation, and proposed a confirm test and a
disconfirm test. You'll now see what actually happened when those tests were run for real -
including cases where a test was refused rather than executed. You'll also see an independent cold
critique of your original claim (Skeptic), including specific strategies Skeptic proposed for
disproving it.

IMPORTANT constraint: this SUT will refuse to execute any test whose text contains a run of letters
longer than {MAX_SAFE_ALPHA_RUN} characters, regardless of which letters or whether they repeat, to
avoid hanging indefinitely on an untested, exponentially-slower input. Design any new test within
that constraint - a test that gets refused again tells you nothing new.

Decide:
1. Is the hypothesis corroborated (survived a real disconfirmation attempt), refuted (contradicted
   by what actually happened), or inconclusive (a test was refused, or didn't actually discriminate)?
2. If not corroborated, is there a next test worth actually running? If so, propose one - a literal
   request body, not a description - designed to make progress given what you now know. Try to
   operationalize one of Skeptic's disproof strategies as a real, executable test (a literal request
   body whose outcome you can observe as fast/slow) if any of them can be expressed that way. If
   none of Skeptic's strategies are testable through this endpoint (e.g. they require server-side
   profiling or code changes you don't have access to), say so explicitly and design the best test
   you can from the test history instead - don't just resubmit a shorter version of a prior idea if
   a different angle, especially one Skeptic raised, would be more informative.

Call submit_followup with your answer."""


def validate_followup(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("verdict", "reasoning", "continue_investigation"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if data.get("verdict") not in ("corroborated", "refuted", "inconclusive"):
        errors.append("'verdict' must be one of corroborated/refuted/inconclusive")

    if not isinstance(data.get("continue_investigation"), bool):
        errors.append("'continue_investigation' must be a boolean")

    if data.get("continue_investigation"):
        next_test = data.get("next_test")
        if not isinstance(next_test, dict):
            errors.append("'next_test' must be an object when continue_investigation is true")
        else:
            if not isinstance(next_test.get("text"), str):
                errors.append("'next_test.text' must be a string")
            if next_test.get("predicted_latency_class") not in ("fast", "slow"):
                errors.append("'next_test.predicted_latency_class' must be 'fast' or 'slow'")
            if "predicted_outcome" not in next_test:
                errors.append("'next_test' missing required field 'predicted_outcome'")

    return errors


def get_followup(client: Anthropic, hypothesis: dict, skeptic_review: dict, history: list[dict]) -> dict:
    evidence = {
        "claim": redact_pathological_content(hypothesis["claim"]),
        "competing_explanation": redact_pathological_content(hypothesis["competing_explanation"]),
        "skeptic_disproof_strategies": [redact_pathological_content(s) for s in skeptic_review["disproof_strategies"]],
        "skeptic_reasoning": redact_pathological_content(skeptic_review["reasoning"]),
        "rounds_so_far": redact_history_for_model(history),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=FOLLOWUP_SYSTEM_PROMPT,
        tools=[FOLLOWUP_TOOL],
        tool_name="submit_followup",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_followup,
        max_tokens=1536,
    )


def run_investigation(anthropic_client: Anthropic, http_client: httpx.Client, calls: list[dict], baseline_model: tuple[float, float]) -> dict:
    """Everything that happens once an anomaly has been found: hypothesis formation,
    a cold Skeptic review, real confirm/disconfirm execution, and a bounded
    follow-up loop informed by real outcomes and by Skeptic's disproof strategies."""
    result = {}

    print("Asking Claude for a hypothesis...")
    hypothesis = get_hypothesis(anthropic_client, calls)
    result["hypothesis"] = hypothesis

    print("Asking Skeptic for a cold review (claim + competing_explanation only)...")
    skeptic_review = get_skeptic_review(anthropic_client, hypothesis["claim"], hypothesis["competing_explanation"])
    result["skeptic_review"] = skeptic_review
    print(f"  skeptic verdict: {skeptic_review['skeptic_verdict']}, competing_explanation assessed as: {skeptic_review['competing_explanation_assessment']}")

    print("Executing confirm_test against the live SUT...")
    confirm_result = execute_test(http_client, hypothesis["confirm_test"], baseline_model)
    result["confirm_result"] = confirm_result

    print("Executing disconfirm_test against the live SUT...")
    disconfirm_result = execute_test(http_client, hypothesis["disconfirm_test"], baseline_model)
    result["disconfirm_result"] = disconfirm_result

    history = [
        {"round": "confirm_test", **confirm_result},
        {"round": "disconfirm_test", **disconfirm_result},
    ]
    followup_rounds = []
    stopped_reason = "round_cap_reached"
    for round_num in range(1, MAX_FOLLOWUP_ROUNDS + 1):
        print(f"Asking Claude for a verdict + follow-up (round {round_num})...")
        followup = get_followup(anthropic_client, hypothesis, skeptic_review, history)
        print(f"  verdict: {followup['verdict']} - continue: {followup['continue_investigation']}")

        round_entry = {
            "verdict": followup["verdict"],
            "reasoning": followup["reasoning"],
            "continue_investigation": followup["continue_investigation"],
        }
        if not followup["continue_investigation"]:
            followup_rounds.append(round_entry)
            stopped_reason = "satisfied"
            break

        print(f"  executing follow-up test (round {round_num})...")
        test_result = execute_test(http_client, followup["next_test"], baseline_model)
        round_entry["test_result"] = test_result
        followup_rounds.append(round_entry)
        history.append({"round": f"followup_{round_num}", **test_result})

    result["followup_rounds"] = followup_rounds
    # "round_cap_reached" here means the last executed follow-up test (if any) in
    # followup_rounds never got a verdict rendered on it - the loop ran out of
    # rounds while the model still wanted to continue investigating.
    result["followup_stopped_reason"] = stopped_reason
    return result


BUG_REPORT_TOOL = {
    "name": "submit_bug_report",
    "description": "Write a bug report summarizing what was found, for a human engineer to read and act on.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "A short, specific summary of the bug."},
            "description": {"type": "string", "description": "What's actually wrong, in plain terms."},
            "steps_to_reproduce": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Concrete, literal steps a human could follow to reproduce this - the actual request(s) to send.",
            },
            "expected_behavior": {"type": "string"},
            "actual_behavior": {"type": "string"},
            "severity": {"type": "string", "enum": ["high", "medium", "low"]},
            "status": {
                "type": "string",
                "enum": ["corroborated", "refuted", "inconclusive"],
                "description": "How well-supported this is by the actual test results. Never claim 'proven' - only corroborated/refuted/inconclusive.",
            },
            "caveats": {
                "type": "string",
                "description": "Any remaining doubts, unresolved Skeptic objections, or limitations of the evidence gathered.",
            },
        },
        "required": [
            "title",
            "description",
            "steps_to_reproduce",
            "expected_behavior",
            "actual_behavior",
            "severity",
            "status",
            "caveats",
        ],
    },
}

BUG_REPORT_SYSTEM_PROMPT = """You are writing a bug report for a human engineer, based on a completed
investigation into an anomaly in a system under test. Unlike earlier steps in this investigation, you
have full access to the actual literal evidence here - the real request text involved and the real,
measured outcomes - because this report needs to be concretely actionable, not abstracted.

Write a clear, honest bug report:
- title/description: what's actually wrong, in plain terms
- steps_to_reproduce: the literal request(s) a human could send to reproduce this themselves
- expected_behavior vs actual_behavior: what should have happened vs what did
- severity: your honest assessment
- status: corroborated, refuted, or inconclusive - reflecting how well this investigation's own
  confirm/disconfirm/follow-up testing actually supports it. Never claim something is "proven."
- caveats: any real doubts remaining - especially any Skeptic objection that was never actually
  settled by a real test, or anything the investigation left unresolved

Call submit_bug_report with your answer."""


def validate_bug_report(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    required = [
        "title",
        "description",
        "steps_to_reproduce",
        "expected_behavior",
        "actual_behavior",
        "severity",
        "status",
        "caveats",
    ]
    for key in required:
        if key not in data:
            errors.append(f"missing required field '{key}'")

    steps = data.get("steps_to_reproduce")
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        errors.append("'steps_to_reproduce' must be a list of strings")

    if data.get("severity") not in ("high", "medium", "low"):
        errors.append("'severity' must be one of high/medium/low")

    if data.get("status") not in ("corroborated", "refuted", "inconclusive"):
        errors.append("'status' must be one of corroborated/refuted/inconclusive")

    return errors


def get_bug_report(client: Anthropic, investigation: dict) -> dict:
    # Deliberately NOT redacted: the whole point is a human-actionable artifact, and
    # the "keep the model blind to the literal pattern" concern only applied to
    # hypothesis formation, not to documenting an already-completed investigation.
    evidence = {
        "hypothesis": investigation["hypothesis"],
        "skeptic_review": investigation["skeptic_review"],
        "confirm_result": investigation["confirm_result"],
        "disconfirm_result": investigation["disconfirm_result"],
        "followup_rounds": investigation["followup_rounds"],
        "followup_stopped_reason": investigation["followup_stopped_reason"],
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=BUG_REPORT_SYSTEM_PROMPT,
        tools=[BUG_REPORT_TOOL],
        tool_name="submit_bug_report",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_bug_report,
        max_tokens=1536,
    )


def main():
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY in .env (see .env.example)")

    anthropic_client = Anthropic(api_key=api_key)
    base_dir = Path(__file__).parent
    out_dir = base_dir / "results"

    def write_output(output: dict) -> None:
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / "output.json"
        out_path.write_text(json.dumps(output, indent=2))
        print(f"\nWrote result to {out_path}")

    with httpx.Client() as http_client:
        try:
            http_client.get(SUT_DOCS_URL, timeout=SUT_READY_TIMEOUT)
        except httpx.TransportError:
            raise SystemExit("sut.py isn't running. Start it first: uvicorn sut:app --port 8000")

        print("Building baseline call log from live SUT...")
        calls = build_call_log(http_client)
        baseline_model = fit_baseline_latency_model(calls)

        output = {"calls": calls}
        bug_report = None
        try:
            casting_log, anomaly_entry, gave_up, behavior_checkpoints = run_checkpoint_cycle(
                anthropic_client, http_client, calls, baseline_model
            )
            output["casting_log"] = casting_log
            output["behavior_checkpoints"] = behavior_checkpoints

            if anomaly_entry is None:
                output["anomaly_found"] = False
                output["casting_stopped_reason"] = "gave_up" if gave_up else "checkpoints_exhausted"
            else:
                output["anomaly_found"] = True

                # Build the log to hand to the investigator: the 8 baseline calls plus
                # every actually-executed casting test, in order, so its own
                # reasoning trail (including tests that came back normal) is visible.
                executed_tests = [e for e in casting_log if not e.get("skipped")]
                calls_with_discovery = list(calls)
                for i, entry in enumerate(executed_tests, start=len(calls) + 1):
                    calls_with_discovery.append(
                        {
                            "index": i,
                            "request": entry["request"],
                            "response": {"measured_latency_ms": entry["actual_measured_latency_ms"]},
                        }
                    )

                output.update(run_investigation(anthropic_client, http_client, calls_with_discovery, baseline_model))

                print("Writing bug report...")
                bug_report = get_bug_report(anthropic_client, output)
                bugs_path = out_dir / "bugs.json"
                out_dir.mkdir(exist_ok=True)
                bugs_path.write_text(json.dumps(bug_report, indent=2))
                print(f"Wrote bug report to {bugs_path}")

        except RuntimeError as e:
            print(f"Stopped early: {e}")
            output["error"] = str(e)

    write_output(output)

    report_path = out_dir / "report.html"
    report_path.write_text(render_report(output, bug_report), encoding="utf-8")
    print(f"Wrote report to {report_path}")

    if "error" not in output:
        if output.get("anomaly_found"):
            print("Now score it by hand against rubric.md.")
        else:
            print("No anomaly found. See behavior_checkpoints for the final behavior hypothesis and Skeptic's critique of it.")


if __name__ == "__main__":
    main()
