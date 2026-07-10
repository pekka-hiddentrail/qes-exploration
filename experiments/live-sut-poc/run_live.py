"""
Live version of pattern-detection-poc: instead of a hand-typed mocked sequence, this
actually calls a running sut.py (start it first with `uvicorn sut:app --port 8000`),
gets real latencies for 8 baseline calls plus one pathological call, asks Claude to
notice the pattern and hypothesize as before - but now confirm_test/disconfirm_test
are directly-executable request bodies instead of prose, and the harness actually
POSTs them to the live server and checks whether the real outcome matches Claude's
predicted_latency_class.
"""

import itertools
import json
import os
import time
from pathlib import Path

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

MODEL = "claude-sonnet-4-6"
MAX_ATTEMPTS = 3
SUT_URL = "http://localhost:8000/analyze"
SUT_DOCS_URL = "http://localhost:8000/docs"
SUT_READY_TIMEOUT = 5.0

# sut.py's regex (^(a+)+$) only backtracks catastrophically on runs of the letter 'a'
# specifically - a long run of any OTHER character fails the match almost instantly
# (no backtracking) and is completely safe. The calibrated pathological input has a
# run of 25 'a's; a proposed test with a longer 'a'-run than this is untested and
# could hang the SUT for a very long time, so it's refused rather than executed.
DANGEROUS_CHAR = "a"
MAX_SAFE_REPEAT_RUN = 25

# A flat latency threshold can't tell "genuinely pathological" apart from "long but
# ordinary input" (the SUT's normal cost model is linear in length). Instead, "slow"
# means measured latency is SLOW_MULTIPLIER-x what the empirical baseline rate (from
# the real, just-measured baseline calls) would predict for that input's length, with
# SLOW_THRESHOLD_MS as a floor so short inputs aren't flagged on noise.
SLOW_THRESHOLD_MS = 500
SLOW_MULTIPLIER = 5

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

# Calibrated empirically (see sut.py docstring): 25 'a's + '!' takes ~3.6s on this
# machine via catastrophic regex backtracking, despite unremarkable length (26 chars).
PATHOLOGICAL_TEXT = "a" * 25 + "!"

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


def call_tool_with_retry(client, *, model, system, tools, tool_name, user_message, validate_fn, max_tokens):
    """Shared call->validate->retry loop used by every tool-forced Claude call in this file."""
    last_errors = ["no attempts made"]
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user_message}],
        )

        tool_use = next((block for block in message.content if block.type == "tool_use"), None)
        if tool_use is None:
            last_errors = [f"no tool_use block in response (stop_reason={message.stop_reason})"]
            print(f"  attempt {attempt} produced no tool call: {last_errors} - retrying")
            continue

        errors = validate_fn(tool_use.input)
        if not errors:
            return tool_use.input

        last_errors = errors
        print(f"  attempt {attempt} produced malformed output: {errors} - retrying")

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
    calls = []
    for i, text in enumerate(BASELINE_TEXTS + [PATHOLOGICAL_TEXT], start=1):
        print(f"  calling baseline/probe {i}...")
        result = call_sut(client, text)
        calls.append(
            {
                "index": i,
                "request": {"method": "POST", "path": "/analyze", "body": {"text": text}},
                "response": result,
            }
        )
    return calls


def baseline_rate_ms_per_char(calls: list[dict]) -> float:
    """Empirical worst-case ms/char from the real baseline calls (excludes the pathological one)."""
    rates = [
        c["response"]["measured_latency_ms"] / max(len(c["request"]["body"]["text"]), 1)
        for c in calls[: len(BASELINE_TEXTS)]
    ]
    return max(rates)


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
        "scenario": "9 sequential calls to POST /analyze, a text-scoring endpoint. Same endpoint, same session, no other traffic in between.",
        "calls": calls,
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=[PATTERN_TOOL],
        tool_name="submit_pattern_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_pattern_hypothesis,
        max_tokens=1536,
    )


def longest_run_of(text: str, char: str) -> int:
    return max((len(list(group)) for key, group in itertools.groupby(text) if key == char), default=0)


assert longest_run_of(PATHOLOGICAL_TEXT, DANGEROUS_CHAR) <= MAX_SAFE_REPEAT_RUN, (
    "PATHOLOGICAL_TEXT exceeds the safety ceiling that execute_test() enforces for "
    "proposed tests, but build_call_log() sends it with no runtime guard - if this "
    "constant is ever recalibrated longer, fix this assertion only after confirming "
    "the new value's timing is still bounded (see sut.py)."
)


def classify_latency(measured_ms: float, text_length: int, baseline_rate: float) -> str:
    expected_ms = baseline_rate * text_length
    threshold = max(SLOW_THRESHOLD_MS, expected_ms * SLOW_MULTIPLIER)
    return "slow" if measured_ms > threshold else "fast"


def execute_test(client: httpx.Client, test: dict, baseline_rate: float) -> dict:
    text = test["text"]
    run_length = longest_run_of(text, DANGEROUS_CHAR)
    if run_length > MAX_SAFE_REPEAT_RUN:
        print(
            f"  refusing to execute: longest run of '{DANGEROUS_CHAR}' is {run_length}, "
            f"exceeds safe ceiling of {MAX_SAFE_REPEAT_RUN}"
        )
        return {
            "sent_text": text,
            "predicted_outcome": test["predicted_outcome"],
            "predicted_latency_class": test["predicted_latency_class"],
            "skipped": True,
            "skip_reason": (
                f"longest run of '{DANGEROUS_CHAR}' is {run_length} chars, exceeding the "
                f"calibrated safe ceiling of {MAX_SAFE_REPEAT_RUN} - refused to avoid "
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
    actual_latency_class = classify_latency(result["measured_latency_ms"], len(text), baseline_rate)
    return {
        "sent_text": text,
        "predicted_outcome": test["predicted_outcome"],
        "predicted_latency_class": test["predicted_latency_class"],
        "actual_measured_latency_ms": result["measured_latency_ms"],
        "actual_latency_class": actual_latency_class,
        "prediction_matched": actual_latency_class == test["predicted_latency_class"],
    }


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
            "reasoning": {"type": "string"},
        },
        "required": [
            "skeptic_verdict",
            "competing_explanation_assessment",
            "skeptic_alternative",
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

Call submit_skeptic_review with your answer."""


def validate_skeptic_review(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("skeptic_verdict", "competing_explanation_assessment", "skeptic_alternative", "reasoning"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if data.get("skeptic_verdict") not in ("holds_up", "weak"):
        errors.append("'skeptic_verdict' must be 'holds_up' or 'weak'")

    if data.get("competing_explanation_assessment") not in ("genuine", "strawman"):
        errors.append("'competing_explanation_assessment' must be 'genuine' or 'strawman'")

    return errors


def get_skeptic_review(client: Anthropic, claim: str, competing_explanation: str) -> dict:
    evidence = {"claim": claim, "competing_explanation": competing_explanation}
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SKEPTIC_SYSTEM_PROMPT,
        tools=[SKEPTIC_TOOL],
        tool_name="submit_skeptic_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_skeptic_review,
        max_tokens=1024,
    )


MAX_FOLLOWUP_ROUNDS = 2

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
including cases where a test was refused rather than executed.

IMPORTANT constraint: this SUT will refuse to execute any test whose text contains a run of the
letter 'a' longer than {MAX_SAFE_REPEAT_RUN} characters, to avoid hanging indefinitely on an
untested, exponentially-slower input. Design any new test within that constraint - a test that
gets refused again tells you nothing new.

Decide:
1. Is the hypothesis corroborated (survived a real disconfirmation attempt), refuted (contradicted
   by what actually happened), or inconclusive (a test was refused, or didn't actually discriminate)?
2. If not corroborated, is there a next test worth actually running? If so, propose one - a literal
   request body, not a description - designed to make progress given what you now know (e.g. if a
   test was refused for being too long, don't just resubmit a shorter version of the same idea if a
   different angle would be more informative).

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


def get_followup(client: Anthropic, hypothesis: dict, history: list[dict]) -> dict:
    evidence = {
        "claim": hypothesis["claim"],
        "competing_explanation": hypothesis["competing_explanation"],
        "rounds_so_far": history,
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=FOLLOWUP_SYSTEM_PROMPT,
        tools=[FOLLOWUP_TOOL],
        tool_name="submit_followup",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_followup,
        max_tokens=1024,
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

        print("Building call log from live SUT...")
        calls = build_call_log(http_client)
        rate = baseline_rate_ms_per_char(calls)

        output = {"calls": calls}
        try:
            print("Asking Claude for a hypothesis...")
            hypothesis = get_hypothesis(anthropic_client, calls)
            output["hypothesis"] = hypothesis

            print("Asking Skeptic for a cold review (claim + competing_explanation only)...")
            skeptic_review = get_skeptic_review(anthropic_client, hypothesis["claim"], hypothesis["competing_explanation"])
            output["skeptic_review"] = skeptic_review
            print(f"  skeptic verdict: {skeptic_review['skeptic_verdict']}, competing_explanation assessed as: {skeptic_review['competing_explanation_assessment']}")

            print("Executing confirm_test against the live SUT...")
            confirm_result = execute_test(http_client, hypothesis["confirm_test"], rate)
            output["confirm_result"] = confirm_result

            print("Executing disconfirm_test against the live SUT...")
            disconfirm_result = execute_test(http_client, hypothesis["disconfirm_test"], rate)
            output["disconfirm_result"] = disconfirm_result

            history = [
                {"round": "confirm_test", **confirm_result},
                {"round": "disconfirm_test", **disconfirm_result},
            ]
            followup_rounds = []
            stopped_reason = "round_cap_reached"
            for round_num in range(1, MAX_FOLLOWUP_ROUNDS + 1):
                print(f"Asking Claude for a verdict + follow-up (round {round_num})...")
                followup = get_followup(anthropic_client, hypothesis, history)
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
                test_result = execute_test(http_client, followup["next_test"], rate)
                round_entry["test_result"] = test_result
                followup_rounds.append(round_entry)
                history.append({"round": f"followup_{round_num}", **test_result})

            output["followup_rounds"] = followup_rounds
            # "round_cap_reached" here means the last executed follow-up test (if any)
            # in followup_rounds never got a verdict rendered on it - the loop ran out
            # of rounds while the model still wanted to continue investigating.
            output["followup_stopped_reason"] = stopped_reason

        except RuntimeError as e:
            print(f"Stopped early: {e}")
            output["error"] = str(e)

    write_output(output)
    if "error" not in output:
        print("Now score it by hand against rubric.md.")


if __name__ == "__main__":
    main()
