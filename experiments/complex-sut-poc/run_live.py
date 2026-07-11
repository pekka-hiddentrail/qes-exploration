"""
Second live-SUT PoC: a genuine TOCTOU (check-then-act) race condition in per-client
rate limiting, instead of live-sut-poc's single-request ReDoS bug. The real
vulnerability stays exactly as-is in sut.py, undisclosed - the Driver only ever
learns it exists by triggering it for real.

Onboarding is deliberately different from live-sut-poc too: instead of 8 pre-baked
baseline calls establishing an empirical pattern to infer, the Driver is given the
API's schema documentation (what a real tester reading published docs would know)
plus exactly one real "happy day" call - then has to design and execute its own
tests from there, including discovering on its own whether concurrency is worth
testing at all.

Unlike live-sut-poc, there is no hard split between "blind discovery" and a
separate "investigation" pipeline once something is found. Every checkpoint does
the same thing: propose and execute a real batch of tests, then form a hypothesis
about the system's behavior AND any anomalies noticed (zero, one, or several) -
which a cold Skeptic reviews. A "weak" verdict sends the Driver into another
checkpoint, informed by the critique, to gather more real evidence; "strong_enough"
ends the loop. Whether an anomaly is currently believed to exist doesn't change the
mechanism - the Skeptic's verdict is what decides whether to keep going, uniformly.
If the final hypothesis claims any anomalies, a bug report is written for each.
"""

import itertools
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from report import render_report

# Model-generated text (reasoning, probes) can contain non-ASCII characters that
# the default Windows console codec can't encode, crashing a plain print().
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MODEL = "claude-sonnet-4-6"
MAX_ATTEMPTS = 3

SUT_URL = "http://127.0.0.1:8000/submit"
SUT_DOCS_URL = "http://127.0.0.1:8000/docs"
SUT_READY_TIMEOUT = 5.0

# A request_count larger than this gets refused rather than executed - not because
# it's dangerous the way a long alphabetic run was in live-sut-poc (nothing here
# can hang; each request is a bounded ~50ms of simulated work), just to bound
# resource usage and keep test results a manageable size.
MAX_REQUEST_COUNT = 20

# Total checkpoints in the whole loop before concluding regardless of Skeptic's
# verdict - covers both "still exploring blind" and "still being challenged on an
# anomaly claim," since those are the same mechanism now, not two different phases
# with separate budgets.
MAX_CHECKPOINTS = 4

# The very first checkpoint is the only genuinely "know nothing" moment - nothing
# has been tested or ruled out yet, so it's the right place to spend extra breadth.
FIRST_ROUND_TEST_BUDGET = 10
DEFAULT_TEST_BUDGET = 6

# What a real tester reading published API docs would already know going in -
# structural facts about the endpoint, not anything about the bug itself.
API_SCHEMA_DOC = """POST /submit

Request body:
  client_id: string - identifies the caller for rate-limiting purposes. Requests
    sharing the same client_id count against the same quota.
  payload: string - the content being submitted for processing.
  priority: string ("normal" or "high") - optional, defaults to "normal".

Response body:
  status: string - "accepted" or "rate_limited".
  used: integer - how many requests this client_id has used in its current
    rate-limit window, as of this response.
  limit: integer - the maximum requests allowed per client_id per window.

Rate limiting: each client_id may make up to `limit` requests per rolling window.
Requests beyond the limit return status="rate_limited" instead of being processed."""

HAPPY_DAY_REQUEST = {"client_id": "demo-client", "payload": "Hello, this is a test submission.", "priority": "normal"}


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


def call_sut_once(request_body: dict) -> dict:
    """One real request on its own connection (so concurrent bursts genuinely
    race each other instead of serializing on a shared client)."""
    with httpx.Client() as client:
        response = client.post(SUT_URL, json=request_body, timeout=30.0)
        return {"status": response.status_code, "body": response.json()}


def call_sut_concurrent(request_body: dict, request_count: int) -> list[dict]:
    """Fires request_count requests all at once - a genuine test of whether shared
    server-side state is safe under concurrent access."""
    with ThreadPoolExecutor(max_workers=request_count) as pool:
        return list(pool.map(lambda _: call_sut_once(request_body), range(request_count)))


def call_sut_sequential(request_body: dict, request_count: int) -> list[dict]:
    """Fires request_count requests one at a time, waiting for each response
    before sending the next. This is NOT the same as call_sut_concurrent with a
    request_count of 1 repeated - it's a genuinely different execution mode, and
    the only way to test whether a bug is concurrency-specific: if the same
    misbehavior shows up here too, concurrency isn't the cause."""
    return [call_sut_once(request_body) for _ in range(request_count)]


def get_happy_day_example() -> dict:
    request = {"method": "POST", "path": "/submit", "body": HAPPY_DAY_REQUEST}
    response = call_sut_once(HAPPY_DAY_REQUEST)
    return {"request": request, "response": response}


def unwrap_accidental_json_body(text: str) -> str:
    """Defends against the Driver wrapping a field's value in a JSON envelope
    (e.g. '{"payload": "..."}') instead of returning the raw string."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and len(parsed) == 1:
                (value,) = parsed.values()
                if isinstance(value, str):
                    return value
        except json.JSONDecodeError:
            pass
    return text


def execute_test(test: dict, test_number: int) -> dict:
    request_count = test["request_count"]
    concurrent = test["concurrent"]
    request_body = {
        "client_id": unwrap_accidental_json_body(test["client_id"]),
        "payload": unwrap_accidental_json_body(test["payload"]),
        "priority": test.get("priority", "normal"),
    }
    request = {
        "method": "POST", "path": "/submit", "body": request_body,
        "request_count": request_count, "concurrent": concurrent,
    }

    if request_count > MAX_REQUEST_COUNT:
        print(f"  refusing to execute test #{test_number}: request_count {request_count} exceeds safe ceiling of {MAX_REQUEST_COUNT}")
        return {
            "test_number": test_number,
            "request": request,
            "predicted_outcome": test["predicted_outcome"],
            "predicted_correctness": test["predicted_correctness"],
            "skipped": True,
            "skip_reason": (
                f"request_count {request_count} exceeds the safe ceiling of {MAX_REQUEST_COUNT} "
                "requests - refused to bound resource usage and result size."
            ),
        }

    responses = call_sut_concurrent(request_body, request_count) if concurrent else call_sut_sequential(request_body, request_count)
    accepted_count = sum(1 for r in responses if r["body"].get("status") == "accepted")
    limit = next((r["body"].get("limit") for r in responses if "limit" in r["body"]), None)
    actual_correctness = "overcounted" if (limit is not None and accepted_count > limit) else "correct"
    return {
        "test_number": test_number,
        "request": request,
        "responses": responses,
        "accepted_count": accepted_count,
        "limit": limit,
        "predicted_outcome": test["predicted_outcome"],
        "predicted_correctness": test["predicted_correctness"],
        "actual_correctness": actual_correctness,
        "prediction_matched": actual_correctness == test["predicted_correctness"],
    }


CASTING_TOOL = {
    "name": "submit_casting_round",
    "description": "Propose a batch of tests against the live /submit endpoint.",
    "input_schema": {
        "type": "object",
        "properties": {
            "give_up": {
                "type": "boolean",
                "description": "Set true only if you have no more good ideas worth proposing this round.",
            },
            "reasoning": {
                "type": "string",
                "description": "Your reasoning for this round's batch, per the system prompt's instructions.",
            },
            "candidate_tests": {
                "type": "array",
                "description": (
                    "See the system prompt for how many tests to propose this round. Each is EITHER tied "
                    "to a specific candidate hypothesis (set linked_hypothesis to that theory, stated in "
                    "full) OR a pure edge-case/negative-case probe not tied to any theory (set "
                    "linked_hypothesis to an empty string). Mix both kinds in the same list."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "linked_hypothesis": {"type": "string"},
                        "client_id": {"type": "string", "description": "The client_id to send in the request body."},
                        "payload": {"type": "string", "description": "The payload to send in the request body."},
                        "priority": {"type": "string", "enum": ["normal", "high"]},
                        "request_count": {
                            "type": "integer",
                            "description": "How many requests to send with this exact client_id/payload/priority. 1 for a normal single-request test.",
                        },
                        "concurrent": {
                            "type": "boolean",
                            "description": (
                                "If true, all request_count requests are fired at the same moment - a genuine "
                                "test of whether shared server-side state (like a rate-limit counter) stays "
                                "correct under concurrent access. If false, requests are sent one at a time, "
                                "each waiting for the previous response before the next is sent - a genuine "
                                "sequential test. These are NOT interchangeable: a sequential test, no matter "
                                "how many requests, can never reveal a concurrency bug, since by definition "
                                "that kind of bug only manifests when multiple requests for the same key are "
                                "in flight at the same moment. Conversely, if you want to check whether some "
                                "misbehavior is concurrency-specific or would happen anyway, you need a "
                                "genuinely sequential test (concurrent=false) with request_count > 1, not just "
                                "a single request."
                            ),
                        },
                        "predicted_outcome": {"type": "string", "description": "What you predict will happen and why."},
                        "predicted_correctness": {
                            "type": "string",
                            "enum": ["correct", "overcounted"],
                            "description": (
                                "'correct' if you predict the rate limit will be enforced properly (accepted "
                                "count will not exceed the disclosed limit). 'overcounted' if you predict "
                                "this specific test will cause more requests to be accepted than the limit "
                                "allows."
                            ),
                        },
                    },
                    "required": ["linked_hypothesis", "client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"],
                },
            },
        },
        "required": ["give_up", "reasoning", "candidate_tests"],
    },
}


def casting_system_prompt(test_budget: int, is_first_round: bool) -> str:
    if is_first_round:
        context_instruction = """Nothing is currently flagged as anomalous, and you do not know whether any
bug exists at all. Before proposing anything, think about context: what can you reasonably assume
about this kind of system (a rate-limited submission API) given its apparent purpose, and what bug
classes are commonly seen in this category of implementation (e.g. off-by-one window boundaries,
race conditions/TOCTOU bugs in shared counters, quota not resetting correctly, inconsistent handling
of unusual client_id values, input validation gaps)? Use that to inform your hypotheses - not as a
substitute for testing, but as a reason to prioritize some categories over others when you're
starting from nothing. State this reasoning explicitly."""
    else:
        context_instruction = """You now have real test results, and prior_checkpoint_feedback holds the
previous checkpoint's hypothesis plus Skeptic's cold critique of it. If that hypothesis claimed any
anomalies that Skeptic found weak, prioritize tests that could confirm OR refute those SPECIFIC
claims - operationalize Skeptic's anomaly_critique and gaps into literal tests, not just unrelated
new exploration. If Skeptic flagged the absence of any anomaly claim as premature given what's been
tested, prioritize whatever category it pointed at. Briefly state what you've actually learned so
far (not what's typical for this category in general, but what THIS system has actually shown) and
how that's changing your approach this round."""

    return f"""You are testing a live API endpoint (POST /submit, a rate-limited submission
service) to look for bugs or unexpected behavior. You've been shown the API's schema
documentation and one real "happy day" call.

{context_instruction}

In one round, propose a BATCH of tests - up to {test_budget} total:
1. Candidate hypotheses: think of a few specific, falsifiable theories about possible bugs.
   For each, propose 1-2 concrete test ideas designed to check it - a client_id, payload,
   priority, request_count, concurrent flag, and a prediction of what would happen if that
   specific theory were true. Set linked_hypothesis to the full theory text for these.
2. Pure edge-case probes: also propose some tests not tied to any specific theory - just
   general negative-case/boundary testing instinct (empty client_id, unusual payload content,
   an unrecognized priority value, etc). For these, predict "correct" (the null hypothesis:
   expect the rate limit to be enforced properly) and set linked_hypothesis to an empty string.

Most tests should use request_count=1 (a normal single request). Use request_count > 1 with
concurrent=true specifically when you suspect shared state might not be safe under concurrent
access - a sequential test, no matter how many requests, can never reveal a concurrency bug,
since by definition that kind of bug only manifests when multiple requests for the same key are
in flight at the same moment. Use request_count > 1 with concurrent=false when you want to check
whether some misbehavior is actually concurrency-specific, or would happen anyway even one
request at a time - a single request can't tell you that either.

All of these tests will be executed for real, together, before you see any results - they don't
depend on each other's outcomes, so make each one a genuinely independent check rather than a
refinement of another test in the same batch. You'll see every real result before being asked
for another round, and can refine across rounds then.

If an earlier round's test was refused rather than executed (check for a skip_reason in
tests_tried_in_earlier_rounds) and you still think that specific hypothesis is worth pursuing,
try a substantially reduced request_count in this round before moving on to a different hypothesis.

If you believe you've explored reasonably and have no more good ideas worth proposing, set
give_up to true rather than proposing something arbitrary just to have something to submit.

Prioritize breadth over depth. Before proposing a test, check tests_tried_in_earlier_rounds: if
the same underlying question has already been asked multiple times with consistent results,
treat it as settled - don't ask a third or fourth variant of it unless something specific
suggests the picture has actually changed.

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
    elif not data.get("give_up") and not tests:
        errors.append("'candidate_tests' must be non-empty unless give_up is true")
    else:
        required_test_keys = (
            "linked_hypothesis", "client_id", "payload", "priority",
            "request_count", "concurrent", "predicted_outcome", "predicted_correctness",
        )
        for i, test in enumerate(tests or []):
            if not isinstance(test, dict):
                errors.append(f"candidate_tests[{i}] must be an object")
                continue
            for key in required_test_keys:
                if key not in test:
                    errors.append(f"candidate_tests[{i}] missing '{key}'")
            if "request_count" in test and not isinstance(test["request_count"], int):
                errors.append(f"candidate_tests[{i}].request_count must be an integer")
            if "concurrent" in test and not isinstance(test["concurrent"], bool):
                errors.append(f"candidate_tests[{i}].concurrent must be a boolean")
            if test.get("predicted_correctness") not in ("correct", "overcounted"):
                errors.append(f"candidate_tests[{i}].predicted_correctness must be 'correct' or 'overcounted'")

    return errors


def redact_history_for_model(casting_log: list[dict]) -> list[dict]:
    """No literal content in this SUT needs hiding from the model (unlike
    live-sut-poc's long alphabetic runs) - this just strips round_reasoning/
    checkpoint bookkeeping so evidence stays focused on outcomes, not re-feeding
    the model its own prior reasoning verbatim."""
    redacted = []
    for entry in casting_log:
        redacted.append({k: v for k, v in entry.items() if k not in ("round_reasoning",)})
    return json.loads(json.dumps(redacted))


def get_casting_round(
    client: Anthropic,
    happy_day_example: dict,
    casting_log: list[dict],
    prior_checkpoint_feedback: dict | None = None,
    *,
    test_budget: int = 6,
    is_first_round: bool = False,
) -> dict:
    evidence = {
        "api_schema": API_SCHEMA_DOC,
        "happy_day_example": happy_day_example,
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
                    "been found yet - don't force a claim that isn't there."
                ),
            },
            "untested_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 1-2 things not yet tried, worth trying next.",
                "minItems": 1,
            },
        },
        "required": ["observed_behavior", "anomalies", "untested_areas"],
    },
}

HYPOTHESIS_SYSTEM_PROMPT = """You are characterizing this system's behavior based on real test results
from this session so far. Describe the general behavior pattern, and list any anomalies (possible
bugs) you've noticed - each as a specific, falsifiable claim referencing the test number(s) that
revealed it, your best guess at the mechanism, its severity if true, and a genuine rival explanation
for the same observation (not a strawman). If nothing anomalous has turned up yet, leave anomalies
empty rather than forcing a claim that isn't there. List what's still untested.

Call submit_checkpoint_hypothesis with your answer."""


def validate_hypothesis_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("observed_behavior", "anomalies", "untested_areas"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    anomalies = data.get("anomalies")
    if not isinstance(anomalies, list) or not all(isinstance(a, str) for a in anomalies):
        errors.append("'anomalies' must be a list of strings (may be empty)")
    areas = data.get("untested_areas")
    if not isinstance(areas, list) or not areas or not all(isinstance(a, str) for a in areas):
        errors.append("'untested_areas' must be a non-empty list of strings")
    return errors


def get_checkpoint_hypothesis(client: Anthropic, happy_day_example: dict, casting_log: list[dict]) -> dict:
    evidence = {
        "api_schema": API_SCHEMA_DOC,
        "happy_day_example": happy_day_example,
        "all_tests_this_session": redact_history_for_model(casting_log),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=HYPOTHESIS_SYSTEM_PROMPT,
        tools=[HYPOTHESIS_TOOL],
        tool_name="submit_checkpoint_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_hypothesis_response,
        max_tokens=2048,
    )


SKEPTIC_TOOL = {
    "name": "submit_skeptic_review",
    "description": "Cold-review a checkpoint hypothesis - you have NOT seen the underlying test data. Poke holes in it; don't rubber-stamp it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["weak", "strong_enough"],
                "description": "'weak' if the hypothesis (its behavior characterization and/or any anomaly claims) is not adequately supported yet. 'strong_enough' only if you genuinely have no material objection left.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete gaps, untested areas, or weak assumptions - things that, if tested, might change the picture.",
                "minItems": 2,
            },
            "anomaly_critique": {
                "type": "string",
                "description": (
                    "If any anomalies were claimed: your independent alternative explanation for the same "
                    "observation(s), and whether each claim's own competing explanation was genuine or a "
                    "strawman, plus concrete ways to distinguish rival explanations from the claim. If no "
                    "anomalies were claimed, briefly say whether that absence itself seems premature given "
                    "what's been tested."
                ),
            },
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "gaps", "anomaly_critique", "reasoning"],
    },
}

SKEPTIC_SYSTEM_PROMPT = """You are cold-reviewing a checkpoint hypothesis - you have NOT seen the raw
test data, only the hypothesis itself (its behavior characterization and any anomaly claims). Your
job is to poke holes, not confirm.

Give a verdict: "weak" if the hypothesis is inadequately supported (whether that's an overconfident
behavior characterization, an anomaly claim that isn't well justified, or a suspicious absence of any
anomaly claim given what's been tested), or "strong_enough" only if you genuinely have no material
objection. Identify at least 2 concrete gaps. If anomalies were claimed, give your own independent
alternative explanation and assess whether each one's own competing explanation is genuine or a
strawman - you propose what's worth investigating further, the Driver decides what to actually test.

Call submit_skeptic_review with your answer."""


def validate_skeptic_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("verdict", "gaps", "anomaly_critique", "reasoning"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if data.get("verdict") not in ("weak", "strong_enough"):
        errors.append("'verdict' must be 'weak' or 'strong_enough'")
    gaps = data.get("gaps")
    if not isinstance(gaps, list) or len(gaps) < 2 or not all(isinstance(g, str) for g in gaps):
        errors.append("'gaps' must be a list of at least 2 strings")
    return errors


def get_skeptic_review(client: Anthropic, hypothesis: dict) -> dict:
    evidence = {
        "observed_behavior": hypothesis["observed_behavior"],
        "anomalies": hypothesis["anomalies"],
        "untested_areas": hypothesis["untested_areas"],
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SKEPTIC_SYSTEM_PROMPT,
        tools=[SKEPTIC_TOOL],
        tool_name="submit_skeptic_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_skeptic_response,
        max_tokens=1536,
    )


def run_checkpoint_loop(anthropic_client: Anthropic, happy_day_example: dict, test_counter):
    """One consistent loop for the whole investigation - no hard split between
    "blind discovery" and a separate "investigation" pipeline. Every checkpoint
    proposes and executes a real batch of tests, then forms a hypothesis about the
    system's behavior AND any anomalies noticed (zero, one, or several), which a
    cold Skeptic reviews. "weak" sends the Driver into another checkpoint informed
    by the critique; "strong_enough" ends the loop. Whether an anomaly is currently
    believed to exist doesn't change the mechanism - Skeptic's verdict is what
    decides whether to keep going, uniformly.

    Returns (casting_log, checkpoints, stopped_reason). checkpoints is a list of
    {checkpoint, hypothesis, skeptic_review} in order - the last entry is the final
    one used to decide whether to write bug reports.
    """
    casting_log = []
    checkpoints = []
    prior_feedback = None
    stopped_reason = "checkpoints_exhausted"

    for checkpoint_num in range(1, MAX_CHECKPOINTS + 1):
        is_first_checkpoint = checkpoint_num == 1
        test_budget = FIRST_ROUND_TEST_BUDGET if is_first_checkpoint else DEFAULT_TEST_BUDGET
        print(f"Asking Claude for a casting round (checkpoint {checkpoint_num}, budget {test_budget})...")
        casting = get_casting_round(
            anthropic_client,
            happy_day_example,
            casting_log,
            prior_feedback,
            test_budget=test_budget,
            is_first_round=is_first_checkpoint,
        )

        if casting["give_up"]:
            print(f"  Claude gave up casting: {casting['reasoning']}")
        else:
            print(f"  round reasoning: {casting['reasoning']}")
            for test in casting["candidate_tests"]:
                linked = test["linked_hypothesis"]
                label = f"hypothesis: {linked}" if linked else "edge case"
                test_number = next(test_counter)
                mode = "concurrent" if test["concurrent"] else "sequential"
                print(f"  test #{test_number} ({label}): client_id={test['client_id']!r} request_count={test['request_count']} ({mode}) payload={test['payload']!r}")

                result = execute_test(test, test_number)
                casting_log.append({
                    "checkpoint": checkpoint_num,
                    "round": 1,
                    "round_reasoning": casting["reasoning"],
                    "linked_hypothesis": linked,
                    **result,
                })
                if result.get("skipped"):
                    print("    refused as unsafe - no signal, continuing")
                else:
                    print(f"    accepted {result['accepted_count']}/{test['request_count']} (limit {result['limit']}) - {result['actual_correctness']}")

        print(f"Checkpoint {checkpoint_num}: forming a hypothesis...")
        hypothesis = get_checkpoint_hypothesis(anthropic_client, happy_day_example, casting_log)
        print(f"  observed_behavior: {hypothesis['observed_behavior']}")
        print(f"  anomalies noticed: {len(hypothesis['anomalies'])}")

        print("Asking Skeptic for a cold review...")
        skeptic_review = get_skeptic_review(anthropic_client, hypothesis)
        print(f"  skeptic verdict: {skeptic_review['verdict']}")

        checkpoints.append({"checkpoint": checkpoint_num, "hypothesis": hypothesis, "skeptic_review": skeptic_review})

        if skeptic_review["verdict"] == "strong_enough":
            stopped_reason = "skeptic_satisfied"
            break

        prior_feedback = {"hypothesis": hypothesis, "skeptic_review": skeptic_review}

    return casting_log, checkpoints, stopped_reason


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
literal, concrete repro steps (real client_id/payload/request_count/concurrent values that actually
reproduced the issue, referencing real test numbers) - each report needs to be independently
actionable, not a redacted summary. Be honest in caveats about anything that wasn't fully resolved -
if the checkpoint budget ran out while Skeptic still had objections (stopped_reason is
"checkpoints_exhausted"), say so explicitly rather than overstating confidence, and set status to
"inconclusive" rather than "corroborated".

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


def get_bug_reports(
    client: Anthropic, final_hypothesis: dict, final_skeptic_review: dict, stopped_reason: str, casting_log: list[dict]
) -> list[dict]:
    evidence = {
        "final_hypothesis": final_hypothesis,
        "final_skeptic_review": final_skeptic_review,
        "stopped_reason": stopped_reason,
        "all_tests_this_session": redact_history_for_model(casting_log),
    }
    result = call_tool_with_retry(
        client,
        model=MODEL,
        system=BUG_REPORT_SYSTEM_PROMPT,
        tools=[BUG_REPORT_TOOL],
        tool_name="submit_bug_reports",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_bug_reports,
        max_tokens=3072,
    )
    return result["bugs"]


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

    try:
        httpx.get(SUT_DOCS_URL, timeout=SUT_READY_TIMEOUT)
    except httpx.TransportError:
        raise SystemExit("sut.py isn't running. Start it first: uvicorn sut:app --port 8000")

    print("Fetching the one happy-day example from the live SUT...")
    happy_day_example = get_happy_day_example()
    print(f"  {happy_day_example['request']['body']} -> {happy_day_example['response']['body']}")

    output = {"api_schema": API_SCHEMA_DOC, "happy_day_example": happy_day_example}
    bug_reports = []
    test_counter = itertools.count(1)
    try:
        casting_log, checkpoints, stopped_reason = run_checkpoint_loop(anthropic_client, happy_day_example, test_counter)
        output["casting_log"] = casting_log
        output["checkpoints"] = checkpoints
        output["stopped_reason"] = stopped_reason

        final_hypothesis = checkpoints[-1]["hypothesis"]
        final_skeptic_review = checkpoints[-1]["skeptic_review"]
        anomalies = final_hypothesis.get("anomalies", [])
        output["anomaly_found"] = len(anomalies) > 0

        if anomalies:
            plural = "y" if len(anomalies) == 1 else "ies"
            print(f"Writing bug report(s) for {len(anomalies)} anomal{plural}...")
            bug_reports = get_bug_reports(anthropic_client, final_hypothesis, final_skeptic_review, stopped_reason, casting_log)
            bugs_path = out_dir / "bugs.json"
            out_dir.mkdir(exist_ok=True)
            bugs_path.write_text(json.dumps(bug_reports, indent=2))
            print(f"Wrote {len(bug_reports)} bug report(s) to {bugs_path}")

    except RuntimeError as e:
        print(f"Stopped early: {e}")
        output["error"] = str(e)

    write_output(output)

    report_path = out_dir / "report.html"
    report_path.write_text(render_report(output, bug_reports), encoding="utf-8")
    print(f"Wrote report to {report_path}")

    if "error" not in output:
        if output.get("anomaly_found"):
            print("Now score it by hand against rubric.md.")
        else:
            print("No anomaly found. See checkpoints for the final hypothesis and Skeptic's critique of it.")


if __name__ == "__main__":
    main()
