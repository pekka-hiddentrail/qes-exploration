"""
Third live-SUT PoC: a credit-purchase API with a realistic mock payment backend.
Unlike live-sut-poc and complex-sut-poc, no bug is deliberately planted in sut.py -
whether a real bug exists at all, and if so what it is, is genuinely unknown going
in, not something the harness is validated against a known ground truth for.

Same unified checkpoint-loop architecture as complex-sut-poc: every checkpoint
proposes and executes a real batch of tests, forms one hypothesis about the
system's behavior AND any anomalies noticed (zero, one, or several), gets a cold
Skeptic review of that hypothesis, and either continues (Skeptic says "weak") or
concludes (Skeptic says "strong_enough" or the checkpoint cap is reached). If the
final hypothesis claims anomalies, a bug report is written for each.

Onboarding: the API schema (including the documented decline_reason values - real,
published interface facts, not anything about a bug) plus 3 fully-disclosed real
accounts (auth token, card number, expiry, CVV - all of it, not a partial guessing
game) and one executed happy-day example proving the flow works. What's genuinely
unknown and has to be learned through testing: each card's spending capacity (never
revealed in any response, by design - realistic, since real payment gateways don't
reveal a card's exact available balance to the merchant either) and whether the
implementation's many validation rules (auth, authorization, Luhn, expiry, CVV,
credit count, tiered pricing, capacity) actually behave as documented in every case.
"""

import itertools
import json
import os
import sys
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

SUT_URL = "http://127.0.0.1:8000/purchase"
SUT_DOCS_URL = "http://127.0.0.1:8000/docs"
SUT_READY_TIMEOUT = 5.0

# Raised from 1 after the first run confirmed the harness and SUT work end to end.
MAX_CHECKPOINTS = 4

FIRST_ROUND_TEST_BUDGET = 12
DEFAULT_TEST_BUDGET = 8

KNOWN_DECLINE_REASONS = [
    "invalid_auth_token",
    "card_not_authorized",
    "invalid_card_number",
    "expiry_mismatch",
    "expired_card",
    "invalid_cvv",
    "invalid_credit_count",
    "insufficient_funds",
]

# What a real tester reading published API docs would already know going in -
# structural facts and documented error codes, not anything about a bug.
API_SCHEMA_DOC = f"""POST /purchase

Request body:
  auth_token: string - identifies the calling user.
  card_number: string - the card to charge.
  expiry_month: integer (1-12)
  expiry_year: integer
  cvv: string
  credit_count: integer - how many credits to purchase.

Response body:
  status: string - "approved" or "declined".
  decline_reason: string or null - present only if declined. One of:
    {", ".join(KNOWN_DECLINE_REASONS)}
  credits_purchased: integer - 0 if declined.
  total_charged: number - dollars charged, 0 if declined.
  new_credit_balance: integer or null - the user's current total credit balance
    after this transaction (whether or not it was approved). null only if
    auth_token itself couldn't be resolved to any account.
  transaction_id: string or null - present only if approved.

Pricing is tiered by bulk quantity - buying more credits in one transaction may
reduce the price per credit for the whole order. The exact tiers are not
published; buy at different quantities to observe pricing behavior.

Each card has a spending capacity that is never revealed directly in any
response (by design - a real payment gateway doesn't reveal a card's exact
available balance to the merchant either). Capacity has to be inferred by
testing purchases until declines start happening."""

# 3 real, fully-disclosed accounts - not a partial guessing game. What's genuinely
# unknown is each card's spending capacity and whether the implementation's many
# validation rules actually behave as documented.
KNOWN_ACCOUNTS = [
    {"auth_token": "tok_live_9f2c8a41", "card_number": "4111104332181963", "expiry_month": 11, "expiry_year": 2027, "cvv": "482"},
    {"auth_token": "tok_live_7d51e6b0", "card_number": "4111001338908383", "expiry_month": 3, "expiry_year": 2028, "cvv": "915"},
    {"auth_token": "tok_live_c3a9f204", "card_number": "4111637940265421", "expiry_month": 8, "expiry_year": 2028, "cvv": "067"},
]

HAPPY_DAY_REQUEST = {**KNOWN_ACCOUNTS[0], "credit_count": 10}


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
    with httpx.Client() as client:
        response = client.post(SUT_URL, json=request_body, timeout=30.0)
        return {"status": response.status_code, "body": response.json()}


def get_happy_day_example() -> dict:
    request = {"method": "POST", "path": "/purchase", "body": HAPPY_DAY_REQUEST}
    response = call_sut_once(HAPPY_DAY_REQUEST)
    return {"request": request, "response": response}


def unwrap_accidental_json_body(text: str) -> str:
    """Defends against the Driver wrapping a field's value in a JSON envelope
    (e.g. '{"auth_token": "..."}') instead of returning the raw string."""
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
    request_body = {
        "auth_token": unwrap_accidental_json_body(test["auth_token"]),
        "card_number": unwrap_accidental_json_body(test["card_number"]),
        "expiry_month": test["expiry_month"],
        "expiry_year": test["expiry_year"],
        "cvv": unwrap_accidental_json_body(test["cvv"]),
        "credit_count": test["credit_count"],
    }
    request = {"method": "POST", "path": "/purchase", "body": request_body}

    response = call_sut_once(request_body)
    body = response["body"]
    actual_status = body.get("status")
    actual_decline_reason = body.get("decline_reason")

    predicted_status = test["predicted_status"]
    predicted_decline_reason = test.get("predicted_decline_reason") or None
    prediction_matched = (
        actual_status == predicted_status
        and (predicted_status != "declined" or actual_decline_reason == predicted_decline_reason)
    )

    return {
        "test_number": test_number,
        "request": request,
        "response": response,
        "predicted_outcome": test["predicted_outcome"],
        "predicted_status": predicted_status,
        "predicted_decline_reason": predicted_decline_reason,
        "actual_status": actual_status,
        "actual_decline_reason": actual_decline_reason,
        "prediction_matched": prediction_matched,
    }


CASTING_TOOL = {
    "name": "submit_casting_round",
    "description": "Propose a batch of tests against the live /purchase endpoint.",
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
                        "auth_token": {"type": "string"},
                        "card_number": {"type": "string"},
                        "expiry_month": {"type": "integer"},
                        "expiry_year": {"type": "integer"},
                        "cvv": {"type": "string"},
                        "credit_count": {"type": "integer"},
                        "predicted_outcome": {"type": "string", "description": "What you predict will happen and why."},
                        "predicted_status": {"type": "string", "enum": ["approved", "declined"]},
                        "predicted_decline_reason": {
                            "type": "string",
                            "description": "Required (one of the documented decline_reason values) if predicted_status is 'declined'. Empty string if predicting 'approved'.",
                        },
                    },
                    "required": [
                        "linked_hypothesis", "auth_token", "card_number", "expiry_month", "expiry_year",
                        "cvv", "credit_count", "predicted_outcome", "predicted_status", "predicted_decline_reason",
                    ],
                },
            },
        },
        "required": ["give_up", "reasoning", "candidate_tests"],
    },
}


def casting_system_prompt(test_budget: int, is_first_round: bool) -> str:
    if is_first_round:
        context_instruction = """Nothing is currently flagged as anomalous, and you do not know whether any
bug exists at all - this implementation was not deliberately seeded with a bug, so there may genuinely be
none to find. Before proposing anything, think about context: what can you reasonably assume about this
kind of system (a credit-purchase API backed by a mock payment processor) given its apparent purpose, and
what bug classes are commonly seen in this category of implementation (e.g. authorization checks that can
be bypassed by mixing credentials across accounts, off-by-one errors at pricing tier or expiry boundaries,
inconsistent validation ordering that leaks information before authorization is confirmed, rounding errors
in tiered pricing, capacity/balance accounting errors)? State this reasoning explicitly."""
    else:
        context_instruction = """You now have real test results, and prior_checkpoint_feedback holds the
previous checkpoint's hypothesis plus Skeptic's cold critique of it. If that hypothesis claimed any
anomalies that Skeptic found weak, prioritize tests that could confirm OR refute those SPECIFIC claims.
If Skeptic flagged the absence of any anomaly claim as premature given what's been tested, prioritize
whatever category it pointed at. Briefly state what you've actually learned so far and how that's
changing your approach this round."""

    return f"""You are testing a live API endpoint (POST /purchase, a credit-purchase API backed by a
mock payment processor) to look for bugs or unexpected behavior. You've been shown the API's schema
documentation (including the documented decline_reason values), 3 real fully-disclosed accounts
(auth_token, card_number, expiry, cvv - use these directly, nothing about them needs to be guessed),
and one real executed "happy day" purchase.

{context_instruction}

In one round, propose a BATCH of tests - up to {test_budget} total:
1. Candidate hypotheses: think of a few specific, falsifiable theories about possible bugs - e.g.
   mixing one account's auth_token with another's card_number (authorization), boundary values around
   the pricing tiers, malformed or boundary expiry/cvv values, credit_count edge cases, or attempting
   to push a card past its (unknown) capacity to see how declines behave. For each, propose 1-2
   concrete test ideas - a full request plus a prediction of what would happen if that specific theory
   were true. Set linked_hypothesis to the full theory text for these.
2. Pure edge-case probes: also propose tests not tied to any specific theory - general negative-case/
   boundary testing instinct. For these, predict "approved" or the most likely "declined" outcome as
   your null hypothesis and set linked_hypothesis to an empty string.

All of these tests will be executed for real, together, before you see any results - they don't
depend on each other's outcomes, so make each one a genuinely independent check rather than a
refinement of another test in the same batch. You'll see every real result before being asked for
another round, and can refine across rounds then.

If you believe you've explored reasonably and have no more good ideas worth proposing, set give_up
to true rather than proposing something arbitrary just to have something to submit.

Prioritize breadth over depth. Before proposing a test, check tests_tried_in_earlier_rounds: if the
same underlying question has already been asked multiple times with consistent results, treat it as
settled - don't ask a third or fourth variant of it unless something specific suggests the picture has
actually changed.

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
            "linked_hypothesis", "auth_token", "card_number", "expiry_month", "expiry_year",
            "cvv", "credit_count", "predicted_outcome", "predicted_status", "predicted_decline_reason",
        )
        for i, test in enumerate(tests or []):
            if not isinstance(test, dict):
                errors.append(f"candidate_tests[{i}] must be an object")
                continue
            for key in required_test_keys:
                if key not in test:
                    errors.append(f"candidate_tests[{i}] missing '{key}'")
            if test.get("predicted_status") not in ("approved", "declined"):
                errors.append(f"candidate_tests[{i}].predicted_status must be 'approved' or 'declined'")
            if test.get("predicted_status") == "declined" and test.get("predicted_decline_reason") not in KNOWN_DECLINE_REASONS:
                errors.append(f"candidate_tests[{i}].predicted_decline_reason must be one of {KNOWN_DECLINE_REASONS}")

    return errors


def redact_history_for_model(casting_log: list[dict]) -> list[dict]:
    """No literal content in this SUT needs hiding from the model - this just
    strips round_reasoning/checkpoint bookkeeping so evidence stays focused on
    outcomes, not re-feeding the model its own prior reasoning verbatim."""
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
        "known_accounts": KNOWN_ACCOUNTS,
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
        max_tokens=4096 if test_budget <= 8 else 6144,
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
        },
        "required": ["observed_behavior", "anomalies", "untested_areas"],
    },
}

HYPOTHESIS_SYSTEM_PROMPT = """You are characterizing this system's behavior based on real test results
from this session so far. Describe the general behavior pattern, and list any anomalies (possible
bugs) you've noticed - each as a specific, falsifiable claim referencing the test number(s) that
revealed it, your best guess at the mechanism, its severity if true, and a genuine rival explanation
for the same observation (not a strawman). If nothing anomalous has turned up yet, leave anomalies
empty rather than forcing a claim that isn't there - this implementation may genuinely have no bugs.
List what's still untested.

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
        "known_accounts": KNOWN_ACCOUNTS,
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
                "description": "'weak' if the hypothesis (its behavior characterization and/or any anomaly claims) is not adequately supported yet, OR if inference_validity_check found any anomaly whose evidence doesn't actually discriminate its claimed mechanism from a real rival. 'strong_enough' only if you genuinely have no material objection left.",
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete gaps, untested areas, or weak assumptions - things that, if tested, might change the picture.",
                "minItems": 2,
            },
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
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "gaps", "inference_validity_check", "anomaly_critique", "reasoning"],
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

Give a verdict: "weak" if the hypothesis is inadequately supported (an overconfident behavior
characterization, an anomaly claim that isn't well justified, a suspicious absence of any anomaly claim
given what's been tested, OR an anomaly whose evidence fails the inference_validity_check above), or
"strong_enough" only if you genuinely have no material objection left. Identify at least 2 concrete
gaps. If anomalies were claimed, give your own independent alternative explanation and assess whether
each one's own competing explanation is genuine or a strawman - you propose what's worth investigating
further, the Driver decides what to actually test. Remember this implementation may genuinely have no
bugs - don't manufacture doubt just to have something to say, but don't rubber-stamp a thin
absence-of-anomalies claim either.

Call submit_skeptic_review with your answer."""


def validate_skeptic_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("verdict", "gaps", "inference_validity_check", "anomaly_critique", "reasoning"):
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
        max_tokens=2048,
    )


def run_checkpoint_loop(anthropic_client: Anthropic, happy_day_example: dict, test_counter):
    """One consistent loop for the whole investigation - every checkpoint proposes
    and executes a real batch of tests, then forms a hypothesis about the system's
    behavior AND any anomalies noticed (zero, one, or several), which a cold
    Skeptic reviews. "weak" sends the Driver into another checkpoint informed by
    the critique; "strong_enough" ends the loop.

    Returns (casting_log, checkpoints, stopped_reason).
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
                print(f"  test #{test_number} ({label}): auth_token={test['auth_token']!r} card={test['card_number']!r} credit_count={test['credit_count']}")

                result = execute_test(test, test_number)
                casting_log.append({
                    "checkpoint": checkpoint_num,
                    "round": 1,
                    "round_reasoning": casting["reasoning"],
                    "linked_hypothesis": linked,
                    **result,
                })
                print(f"    actual: {result['actual_status']}" + (f" ({result['actual_decline_reason']})" if result['actual_decline_reason'] else "") + f" - prediction {'matched' if result['prediction_matched'] else 'MISSED'}")

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
literal, concrete repro steps (real auth_token/card_number/expiry/cvv/credit_count values that
actually reproduced the issue, referencing real test numbers) - each report needs to be independently
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

    output = {"api_schema": API_SCHEMA_DOC, "known_accounts": KNOWN_ACCOUNTS, "happy_day_example": happy_day_example}
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
