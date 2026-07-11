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

Casting is broken into small checkpoints exactly as in live-sut-poc: if a
checkpoint's rounds find nothing anomalous, the Driver characterizes behavior so
far and a cold Skeptic critiques it before the next checkpoint proceeds. The
moment any test's burst shows more accepted requests than the disclosed rate
limit allows, casting stops and hands off to formal hypothesis formation, a
separate Skeptic review, confirm/disconfirm execution, a bounded follow-up loop,
and a bug report - all unchanged in spirit from live-sut-poc, adapted to this
SUT's request/response shape.
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

ROUNDS_PER_CHECKPOINT = 2
MAX_CHECKPOINTS = 2
MAX_FOLLOWUP_ROUNDS = 2

# Total hypothesis attempts (initial + revisions) before proceeding to
# confirm/disconfirm regardless of Skeptic's verdict. Without this cap, a
# "weak" verdict on the initial claim only ever fed into the *optional*
# follow-up loop, after confirm/disconfirm tests chosen before Skeptic saw
# anything had already spent the test budget - Skeptic's verdict had no actual
# power to stop a shaky claim from being tested as-is. Now a "weak" verdict
# sends the Driver back to revise before any real test budget is spent.
MAX_HYPOTHESIS_ATTEMPTS = 2

# The very first round is the only genuinely "know nothing" moment - nothing has
# been tested or ruled out yet, so it's the right place to spend extra breadth.
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
        context_instruction = """Before proposing anything, think about context: what can you reasonably assume
about this kind of system (a rate-limited submission API) given its apparent purpose, and what bug
classes are commonly seen in this category of implementation (e.g. off-by-one window boundaries,
race conditions/TOCTOU bugs in shared counters, quota not resetting correctly, inconsistent handling
of unusual client_id values, input validation gaps)? Use that to inform your hypotheses - not as a
substitute for testing, but as a reason to prioritize some categories over others when you're
starting from nothing. State this reasoning explicitly."""
    else:
        context_instruction = """You now have real test results, not just assumptions about this kind of system.
Before proposing anything, briefly state what you've actually learned so far (not what's typical for
this category in general, but what THIS system has actually shown) and how that's changing your
approach this round - narrowing toward what looks promising, or ruling out categories that turned
out unremarkable."""

    return f"""You are testing a live API endpoint (POST /submit, a rate-limited submission
service) to look for bugs or unexpected behavior. You've been shown the API's schema
documentation and one real "happy day" call. Nothing is currently flagged as anomalous, and
you do not know whether any bug exists at all.

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

If "prior_checkpoint_feedback" is present in the evidence, it's an independent cold critique of
a behavior characterization from your last checkpoint - including gaps and untested areas it
flagged. Use it to inform what you prioritize this round, though you still decide what to
actually test, not the critique itself.

Prioritize breadth over depth. Before proposing a test, check tests_tried_in_earlier_rounds: if
the same underlying question has already been asked multiple times with consistent results,
treat it as settled - don't ask a third or fourth variant of it unless something specific
suggests the picture has actually changed. When prior_checkpoint_feedback names gaps,
prioritize genuinely new categories over refining a theory that already has consistent
supporting evidence.

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


BEHAVIOR_HYPOTHESIS_TOOL = {
    "name": "submit_behavior_hypothesis",
    "description": "Characterize what you've learned about this system's behavior so far - not a bug claim, a general characterization.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_behavior": {
                "type": "string",
                "description": "General characterization: confirmed patterns, categories tested and found normal, how the rate limit behaves under the conditions you've tried.",
            },
            "untested_areas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 1-2 things not yet tried, worth trying next.",
                "minItems": 1,
            },
        },
        "required": ["observed_behavior", "untested_areas"],
    },
}

BEHAVIOR_HYPOTHESIS_SYSTEM_PROMPT = """You are characterizing a system's behavior so far, based on
real test results from this session. You have NOT found anything anomalous yet - this is not a bug
claim, it's an honest summary of what you've learned and what remains untested.

Call submit_behavior_hypothesis with your answer."""


def validate_behavior_hypothesis(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("observed_behavior", "untested_areas"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    areas = data.get("untested_areas")
    if not isinstance(areas, list) or not areas or not all(isinstance(a, str) for a in areas):
        errors.append("'untested_areas' must be a non-empty list of strings")
    return errors


def get_behavior_hypothesis(client: Anthropic, happy_day_example: dict, casting_log: list[dict]) -> dict:
    evidence = {
        "api_schema": API_SCHEMA_DOC,
        "happy_day_example": happy_day_example,
        "tests_executed_this_session": redact_history_for_model(casting_log),
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
        "observed_behavior": behavior_hypothesis["observed_behavior"],
        "untested_areas": behavior_hypothesis["untested_areas"],
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


def run_checkpoint_cycle(anthropic_client: Anthropic, happy_day_example: dict, test_counter):
    """Casting broken into small checkpoints. Each checkpoint runs up to
    ROUNDS_PER_CHECKPOINT rounds (same batch-propose-and-execute mechanism, same
    early-exit the moment a test's burst shows more accepted requests than the
    disclosed limit allows). If a checkpoint's rounds find nothing, the Driver is
    forced to characterize the SUT's behavior so far and Skeptic critiques it -
    guaranteeing Skeptic gets exercised even when discovery fails outright - and
    that critique feeds into the next checkpoint's rounds.

    Returns (casting_log, anomaly_entry, gave_up, behavior_checkpoints).
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
                happy_day_example,
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
                linked = test["linked_hypothesis"]
                label = f"hypothesis: {linked}" if linked else "edge case"
                test_number = next(test_counter)
                mode = "concurrent" if test["concurrent"] else "sequential"
                print(f"  test #{test_number} ({label}): client_id={test['client_id']!r} request_count={test['request_count']} ({mode}) payload={test['payload']!r}")

                result = execute_test(test, test_number)
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

                print(f"    accepted {result['accepted_count']}/{test['request_count']} (limit {result['limit']}) - {result['actual_correctness']}")
                if result["actual_correctness"] == "overcounted" and anomaly_entry is None:
                    print(f"    anomaly found ({label})")
                    anomaly_entry = entry

            if anomaly_entry is not None:
                break

        if anomaly_entry is not None:
            return casting_log, anomaly_entry, False, behavior_checkpoints

        print(f"Checkpoint {checkpoint_num}: nothing found, asking for a behavior hypothesis...")
        behavior_hypothesis = get_behavior_hypothesis(anthropic_client, happy_day_example, casting_log)
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
        prior_feedback = {
            "behavior_hypothesis": behavior_hypothesis,
            "behavior_skeptic_review": behavior_skeptic_review,
        }

    return casting_log, None, gave_up_this_checkpoint, behavior_checkpoints


PATTERN_TOOL = {
    "name": "submit_hypothesis",
    "description": "Submit your falsifiable claim about the anomaly, plus a genuine competing explanation and real confirm/disconfirm tests.",
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_pattern": {"type": "string", "description": "How the system has behaved overall, across everything tested so far."},
            "anomalous_test_number": {"type": "integer", "description": "The test_number of the test that revealed the anomaly."},
            "claim": {"type": "string", "description": "Your specific, falsifiable explanation for why that test overcounted accepted requests."},
            "competing_explanation": {"type": "string", "description": "A genuine rival explanation for the same observation - not a strawman you'd easily dismiss."},
            "severity_if_true": {"type": "string", "enum": ["low", "medium", "high"]},
            "confirm_test": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "payload": {"type": "string"},
                    "priority": {"type": "string", "enum": ["normal", "high"]},
                    "request_count": {"type": "integer"},
                    "concurrent": {"type": "boolean", "description": "true = all requests fired at once; false = sent one at a time, each waiting for the previous response."},
                    "predicted_outcome": {"type": "string"},
                    "predicted_correctness": {"type": "string", "enum": ["correct", "overcounted"]},
                },
                "required": ["client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"],
                "description": "A test designed to reproduce the anomaly again, isolated from anything else.",
            },
            "disconfirm_test": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "payload": {"type": "string"},
                    "priority": {"type": "string", "enum": ["normal", "high"]},
                    "request_count": {"type": "integer"},
                    "concurrent": {"type": "boolean", "description": "true = all requests fired at once; false = sent one at a time, each waiting for the previous response."},
                    "predicted_outcome": {"type": "string"},
                    "predicted_correctness": {"type": "string", "enum": ["correct", "overcounted"]},
                },
                "required": ["client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"],
                "description": (
                    "A test genuinely designed to try to PROVE YOUR CLAIM WRONG - not a weaker version of the "
                    "confirm test. If your claim is specifically that CONCURRENCY causes the bug, a genuine "
                    "disconfirm test needs concurrent=false with request_count > 1 (a real sequential test, not "
                    "just one request) - that's the only way to check whether the same misbehavior happens "
                    "even without concurrency, which would refute a concurrency-specific claim."
                ),
            },
        },
        "required": ["observed_pattern", "anomalous_test_number", "claim", "competing_explanation", "severity_if_true", "confirm_test", "disconfirm_test"],
    },
}

SYSTEM_PROMPT = """You found a test in this session whose burst showed more accepted requests than
the disclosed rate limit allows. Now form a specific, falsifiable claim about WHY - not just "the
rate limit is broken" but the actual mechanism (e.g. a check-then-act race condition, a counter that
isn't updated atomically, etc).

You must also propose a genuine competing explanation - a real rival hypothesis, not a strawman you'd
easily dismiss (e.g. "it was a fluke/random server load" is a fair rival unless you can rule it out).

Design a confirm_test that should reproduce the anomaly, and a disconfirm_test that genuinely tries to
prove your claim WRONG - not a weaker version of the same test. Both will be executed for real.

Call submit_hypothesis with your answer."""


def validate_hypothesis(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("observed_pattern", "anomalous_test_number", "claim", "competing_explanation", "severity_if_true", "confirm_test", "disconfirm_test"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if data.get("severity_if_true") not in ("low", "medium", "high"):
        errors.append("'severity_if_true' must be low/medium/high")
    for test_key in ("confirm_test", "disconfirm_test"):
        test = data.get(test_key)
        if not isinstance(test, dict):
            errors.append(f"'{test_key}' must be an object")
            continue
        for field in ("client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"):
            if field not in test:
                errors.append(f"'{test_key}' missing '{field}'")
        if test.get("predicted_correctness") not in ("correct", "overcounted"):
            errors.append(f"'{test_key}.predicted_correctness' must be 'correct' or 'overcounted'")
    return errors


def get_hypothesis(client: Anthropic, happy_day_example: dict, casting_log: list[dict]) -> dict:
    evidence = {
        "api_schema": API_SCHEMA_DOC,
        "happy_day_example": happy_day_example,
        "all_tests_this_session": redact_history_for_model(casting_log),
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=SYSTEM_PROMPT,
        tools=[PATTERN_TOOL],
        tool_name="submit_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_hypothesis,
        max_tokens=2048,
    )


REVISE_HYPOTHESIS_SYSTEM_PROMPT = """Skeptic has cold-reviewed your claim and found it weak - before
any real test budget is spent on confirm/disconfirm, you must revise.

Directly address what Skeptic found weak: either strengthen the justification for the SAME claim if
you still believe it's right (engage specifically with Skeptic's reasoning, don't just restate the
claim), or pivot to a different, better-supported claim informed by Skeptic's own alternative
explanation and disproof strategies. Design new confirm_test and disconfirm_test that reflect
whatever claim you land on - don't resubmit the same tests unchanged if the claim itself changed, and
make sure the disconfirm test could genuinely refute the (possibly revised) claim, not a strawman of
it.

Call submit_hypothesis with your revised answer."""


def revise_hypothesis(
    client: Anthropic, happy_day_example: dict, casting_log: list[dict], prior_hypothesis: dict, skeptic_review: dict
) -> dict:
    evidence = {
        "api_schema": API_SCHEMA_DOC,
        "happy_day_example": happy_day_example,
        "all_tests_this_session": redact_history_for_model(casting_log),
        "your_prior_hypothesis": prior_hypothesis,
        "skeptics_critique": skeptic_review,
    }
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=REVISE_HYPOTHESIS_SYSTEM_PROMPT,
        tools=[PATTERN_TOOL],
        tool_name="submit_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_hypothesis,
        max_tokens=2048,
    )


SKEPTIC_TOOL = {
    "name": "submit_skeptic_review",
    "description": "Cold-review a claim and its competing explanation - you have NOT seen the underlying test data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "skeptic_verdict": {"type": "string", "enum": ["holds_up", "weak"]},
            "competing_explanation_assessment": {"type": "string", "enum": ["genuine", "strawman"]},
            "skeptic_alternative": {"type": "string", "description": "Your own independent alternative explanation, not a restatement of the given competing explanation."},
            "disproof_strategies": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2-3 concrete strategies for trying to disprove the claim.",
                "minItems": 2,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["skeptic_verdict", "competing_explanation_assessment", "skeptic_alternative", "disproof_strategies", "reasoning"],
    },
}

SKEPTIC_SYSTEM_PROMPT = """You are cold-reviewing a claim and its competing explanation. You have NOT
seen the underlying test data - only the claim and competing explanation themselves. Your job is to
poke holes, not confirm.

Assess whether the claim holds up or is weak given what it's actually built on. Assess whether the
given competing explanation is genuine or a strawman. Propose your OWN independent alternative
explanation - not a restatement of the one given. Propose at least 2-3 concrete disproof strategies -
you propose what's worth trying, the Driver decides what to actually test.

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
    evidence = {"claim": claim, "competing_explanation": competing_explanation}
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
    "description": "Give a verdict on the claim given everything so far, and (if continuing) the next test to run.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["corroborated", "refuted", "inconclusive"]},
            "reasoning": {"type": "string"},
            "continue_investigation": {"type": "boolean"},
            "next_test": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "payload": {"type": "string"},
                    "priority": {"type": "string", "enum": ["normal", "high"]},
                    "request_count": {"type": "integer"},
                    "concurrent": {"type": "boolean", "description": "true = all requests fired at once; false = sent one at a time, each waiting for the previous response."},
                    "predicted_outcome": {"type": "string"},
                    "predicted_correctness": {"type": "string", "enum": ["correct", "overcounted"]},
                },
                "required": ["client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"],
                "description": "Required if continue_investigation is true, omitted otherwise.",
            },
        },
        "required": ["verdict", "reasoning", "continue_investigation"],
    },
}

FOLLOWUP_SYSTEM_PROMPT = f"""Given the claim, Skeptic's review, and the real confirm/disconfirm/
follow-up results so far, give your verdict: corroborated, refuted, or inconclusive. Consider
Skeptic's disproof_strategies as ideas for what to test next, though you decide the actual test.

Remember concurrent and request_count are independent: concurrent=true fires every request at the
same moment (the only way to test shared-state safety under real concurrency); concurrent=false
sends them one at a time, each waiting for the previous response (a genuine sequential test, not
the same as a single request repeated). If you need to check whether a concurrency-specific claim
would also fail sequentially, that requires concurrent=false with request_count > 1 - a single
request can't answer that question.

Requests larger than {MAX_REQUEST_COUNT} get refused rather than executed - if an earlier test was
refused for that reason and you still want to pursue it, try a smaller request_count.

If continue_investigation is true, propose the single next test that would most efficiently refine
the verdict. If you're satisfied either way (corroborated with enough evidence, or refuted), set
continue_investigation to false and omit next_test.

Call submit_followup with your answer."""


def validate_followup(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("verdict", "reasoning", "continue_investigation"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if data.get("verdict") not in ("corroborated", "refuted", "inconclusive"):
        errors.append("'verdict' must be corroborated/refuted/inconclusive")
    if data.get("continue_investigation") is True:
        next_test = data.get("next_test")
        if not isinstance(next_test, dict):
            errors.append("'next_test' is required (as an object) when continue_investigation is true")
        else:
            for field in ("client_id", "payload", "priority", "request_count", "concurrent", "predicted_outcome", "predicted_correctness"):
                if field not in next_test:
                    errors.append(f"'next_test' missing '{field}'")
            if next_test.get("predicted_correctness") not in ("correct", "overcounted"):
                errors.append("'next_test.predicted_correctness' must be 'correct' or 'overcounted'")
    return errors


def get_followup(client: Anthropic, hypothesis: dict, skeptic_review: dict, history: list[dict]) -> dict:
    evidence = {
        "claim": hypothesis["claim"],
        "competing_explanation": hypothesis["competing_explanation"],
        "skeptic_review": skeptic_review,
        "history": history,
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


def run_investigation(anthropic_client: Anthropic, happy_day_example: dict, casting_log: list[dict], test_counter) -> dict:
    """Everything that happens once an anomaly has been found: hypothesis formation,
    a cold Skeptic review, real confirm/disconfirm execution, and a bounded
    follow-up loop informed by real outcomes and by Skeptic's disproof strategies.

    Skeptic's initial review is a real gate, not just advisory context for later:
    a "weak" verdict sends the Driver back to revise the claim (and its
    confirm/disconfirm tests) before any real test budget is spent, up to
    MAX_HYPOTHESIS_ATTEMPTS total attempts. Without this, confirm/disconfirm tests
    chosen before Skeptic ever saw the claim would execute regardless of the
    verdict, and "weak" only ever reached the Driver as one more piece of context
    in the optional follow-up loop - after the tests it should have prevented had
    already run."""
    result = {}

    print("Asking Claude for a hypothesis...")
    hypothesis = get_hypothesis(anthropic_client, happy_day_example, casting_log)

    hypothesis_attempts = []
    skeptic_review = None
    for attempt in range(1, MAX_HYPOTHESIS_ATTEMPTS + 1):
        print(f"Asking Skeptic for a cold review (attempt {attempt}, claim + competing_explanation only)...")
        skeptic_review = get_skeptic_review(anthropic_client, hypothesis["claim"], hypothesis["competing_explanation"])
        print(f"  skeptic verdict: {skeptic_review['skeptic_verdict']}, competing_explanation assessed as: {skeptic_review['competing_explanation_assessment']}")
        hypothesis_attempts.append({"hypothesis": hypothesis, "skeptic_review": skeptic_review})

        if skeptic_review["skeptic_verdict"] == "holds_up" or attempt == MAX_HYPOTHESIS_ATTEMPTS:
            break

        print("  Skeptic found the hypothesis weak - asking the Driver to revise before spending test budget...")
        hypothesis = revise_hypothesis(anthropic_client, happy_day_example, casting_log, hypothesis, skeptic_review)

    result["hypothesis"] = hypothesis
    result["skeptic_review"] = skeptic_review
    result["hypothesis_attempts"] = hypothesis_attempts

    print("Executing confirm_test against the live SUT...")
    confirm_result = execute_test(hypothesis["confirm_test"], next(test_counter))
    result["confirm_result"] = confirm_result

    print("Executing disconfirm_test against the live SUT...")
    disconfirm_result = execute_test(hypothesis["disconfirm_test"], next(test_counter))
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
        test_result = execute_test(followup["next_test"], next(test_counter))
        round_entry["test_result"] = test_result
        followup_rounds.append(round_entry)
        history.append({"round": f"followup_{round_num}", **test_result})

    result["followup_rounds"] = followup_rounds
    result["followup_stopped_reason"] = stopped_reason
    return result


BUG_REPORT_TOOL = {
    "name": "submit_bug_report",
    "description": "Write the final bug report - deliberately NOT redacted, since this needs real repro steps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "steps_to_reproduce": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "expected_behavior": {"type": "string"},
            "actual_behavior": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "status": {"type": "string", "enum": ["corroborated", "refuted", "inconclusive"]},
            "caveats": {"type": "string", "description": "Honest caveats about what wasn't resolved or verified."},
        },
        "required": ["title", "description", "steps_to_reproduce", "expected_behavior", "actual_behavior", "severity", "status", "caveats"],
    },
}

BUG_REPORT_SYSTEM_PROMPT = """Write the final bug report for this investigation, based on everything
in the evidence: the claim, Skeptic's review, and the real confirm/disconfirm/follow-up results.
Include literal, concrete repro steps (real client_id/payload/request_count/concurrent values that
actually reproduced the issue) - this report needs to be independently actionable, not a redacted summary.
Be honest in caveats about anything that wasn't fully resolved or verified.

Call submit_bug_report with your answer."""


def validate_bug_report(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("title", "description", "steps_to_reproduce", "expected_behavior", "actual_behavior", "severity", "status", "caveats"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    steps = data.get("steps_to_reproduce")
    if not isinstance(steps, list) or not steps or not all(isinstance(s, str) for s in steps):
        errors.append("'steps_to_reproduce' must be a non-empty list of strings")
    if data.get("severity") not in ("low", "medium", "high"):
        errors.append("'severity' must be low/medium/high")
    if data.get("status") not in ("corroborated", "refuted", "inconclusive"):
        errors.append("'status' must be corroborated/refuted/inconclusive")
    return errors


def get_bug_report(client: Anthropic, investigation: dict) -> dict:
    return call_tool_with_retry(
        client,
        model=MODEL,
        system=BUG_REPORT_SYSTEM_PROMPT,
        tools=[BUG_REPORT_TOOL],
        tool_name="submit_bug_report",
        user_message=json.dumps(investigation, indent=2),
        validate_fn=validate_bug_report,
        max_tokens=2048,
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

    try:
        httpx.get(SUT_DOCS_URL, timeout=SUT_READY_TIMEOUT)
    except httpx.TransportError:
        raise SystemExit("sut.py isn't running. Start it first: uvicorn sut:app --port 8000")

    print("Fetching the one happy-day example from the live SUT...")
    happy_day_example = get_happy_day_example()
    print(f"  {happy_day_example['request']['body']} -> {happy_day_example['response']['body']}")

    output = {"api_schema": API_SCHEMA_DOC, "happy_day_example": happy_day_example}
    bug_report = None
    test_counter = itertools.count(1)
    try:
        casting_log, anomaly_entry, gave_up, behavior_checkpoints = run_checkpoint_cycle(
            anthropic_client, happy_day_example, test_counter
        )
        output["casting_log"] = casting_log
        output["behavior_checkpoints"] = behavior_checkpoints

        if anomaly_entry is None:
            output["anomaly_found"] = False
            output["casting_stopped_reason"] = "gave_up" if gave_up else "checkpoints_exhausted"
        else:
            output["anomaly_found"] = True
            output.update(run_investigation(anthropic_client, happy_day_example, casting_log, test_counter))

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
