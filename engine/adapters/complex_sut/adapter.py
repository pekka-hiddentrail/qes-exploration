"""SUTAdapter for the complex-sut mock: a rate-limited submission API with a
genuine TOCTOU concurrency race (not simulated - FastAPI dispatches sync
handlers across a real thread pool, and time.sleep() releases the GIL, so
concurrent requests for the same client_id really do interleave). Ported
from experiments/complex-sut-poc/run_live.py and report.py.

The second adapter ever built against SUTAdapter, and deliberately the most
different from the first (token_purchase): tests here can mean firing a
BURST of concurrent requests and aggregating multiple responses into one
result, not a single request/response pair - a genuine stress test of
whether the interface (execute_test returning an arbitrary dict,
render_test_entry fully adapter-owned) actually generalizes.
"""

from concurrent.futures import ThreadPoolExecutor

from engine.adapter import SUTAdapter
from engine.http import call_sut_once
from engine.report import badge, bool_badge, esc, inline_markdown, render_json_block
from engine.util import unwrap_accidental_json_body

BASE_URL = "http://127.0.0.1:8000"
TEST_ENDPOINT_PATH = "/submit"

# A burst this large would tie up real resources and produce an unreadably
# huge result for no additional information - refuse rather than execute.
MAX_REQUEST_COUNT = 20

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


def _call_sut_concurrent(request_body: dict, request_count: int) -> list[dict]:
    """Fires request_count requests all at once, each on its own connection -
    a genuine test of whether shared server-side state is safe under
    concurrent access."""
    with ThreadPoolExecutor(max_workers=request_count) as pool:
        return list(pool.map(lambda _: call_sut_once(BASE_URL, TEST_ENDPOINT_PATH, request_body), range(request_count)))


def _call_sut_sequential(request_body: dict, request_count: int) -> list[dict]:
    """Fires request_count requests one at a time, waiting for each response
    before sending the next - the only way to test whether a bug is
    concurrency-specific: if the same misbehavior shows up here too,
    concurrency isn't the cause."""
    return [call_sut_once(BASE_URL, TEST_ENDPOINT_PATH, request_body) for _ in range(request_count)]


def execute_test(test: dict, test_number: int) -> dict:
    request_count = test["request_count"]
    concurrent = test["concurrent"]
    request_body = {
        "client_id": unwrap_accidental_json_body(test["client_id"]),
        "payload": unwrap_accidental_json_body(test["payload"]),
        "priority": test.get("priority", "normal"),
    }
    request = {
        "method": "POST", "path": TEST_ENDPOINT_PATH, "body": request_body,
        "request_count": request_count, "concurrent": concurrent,
    }

    if request_count > MAX_REQUEST_COUNT:
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

    responses = _call_sut_concurrent(request_body, request_count) if concurrent else _call_sut_sequential(request_body, request_count)
    accepted_count = sum(1 for r in responses if r["body"].get("status") == "accepted")
    limit = next((r["body"].get("limit") for r in responses if isinstance(r.get("body"), dict) and "limit" in r["body"]), None)
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


def describe_test_for_log(test: dict) -> str:
    mode = "concurrent" if test["concurrent"] else "sequential"
    return f"client_id={test['client_id']!r} payload={test['payload']!r} request_count={test['request_count']} ({mode})"


def describe_result_for_log(result: dict) -> str:
    if result.get("skipped"):
        return f"SKIPPED - {result['skip_reason']}"
    return f"{result['actual_correctness']} (accepted {result['accepted_count']}/{result['request']['request_count']}, limit {result['limit']})"


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
            # client_id/payload flow straight into unwrap_accidental_json_body(),
            # which calls .strip() on them - a non-string value would crash the
            # run rather than just producing a rejected, resubmit-able test.
            for key in ("linked_hypothesis", "client_id", "payload", "predicted_outcome"):
                if key in test and not isinstance(test[key], str):
                    errors.append(f"candidate_tests[{i}].{key} must be a string")
            if "request_count" in test and not isinstance(test["request_count"], int):
                errors.append(f"candidate_tests[{i}].request_count must be an integer")
            if "concurrent" in test and not isinstance(test["concurrent"], bool):
                errors.append(f"candidate_tests[{i}].concurrent must be a boolean")
            if test.get("predicted_correctness") not in ("correct", "overcounted"):
                errors.append(f"candidate_tests[{i}].predicted_correctness must be 'correct' or 'overcounted'")

    return errors


def _correctness_badge(correctness) -> str:
    if correctness == "correct":
        return badge("correct", "good")
    if correctness == "overcounted":
        return badge("overcounted", "bad")
    return badge(correctness or "unknown", "warn")


def render_test_entry(entry) -> str:
    if not entry:
        return ""
    request = entry.get("request", {})
    body = request.get("body", {})
    request_count = request.get("request_count", 1)
    mode = "concurrent" if request.get("concurrent") else "sequential"
    mode_html = f'<span class="test-number">{esc(request_count)} req &middot; {esc(mode)}</span>'

    linked = entry.get("linked_hypothesis")
    linked_html = f"<strong>Hypothesis:</strong> {esc(linked)}" if linked else (
        '<span class="probe-label">Edge-case probe</span> (no linked hypothesis)'
    )
    test_number = entry.get("test_number")
    number_html = f'<span class="test-number">Test #{esc(test_number)}</span>' if test_number is not None else ""

    if entry.get("skipped"):
        return f"""
        <article class="test test-skipped">
          <div class="test-hypothesis">{number_html}{mode_html}{linked_html}</div>
          {render_json_block(body)}
          <div class="test-outcome">{badge('skipped', 'warn')} {inline_markdown(entry.get('skip_reason'))}</div>
        </article>
        """

    responses = entry.get("responses", [])
    used_values = [r["body"].get("used") for r in responses if isinstance(r.get("body"), dict) and "used" in r["body"]]
    matched = entry.get("prediction_matched")
    return f"""
    <article class="test">
      <div class="test-hypothesis">{number_html}{mode_html}{linked_html}</div>
      {render_json_block(body)}
      <div class="test-predicted">Predicted: {inline_markdown(entry.get('predicted_outcome'))}
        {_correctness_badge(entry.get('predicted_correctness'))}</div>
      <div class="test-outcome">
        Accepted <span class="num">{esc(entry.get('accepted_count'))}/{esc(request_count)}</span>
        (limit <span class="num">{esc(entry.get('limit'))}</span>)
        {_correctness_badge(entry.get('actual_correctness'))}
        <span class="sep">&middot;</span> prediction {bool_badge(matched, 'matched', 'missed')}
      </div>
      <div class="test-outcome prose-muted">used values across responses: <span class="num">{esc(', '.join(str(u) for u in used_values))}</span></div>
    </article>
    """


def render_onboarding_section(api_schema, onboarding_extra, happy_day_example) -> str:
    happy_request = (happy_day_example or {}).get("request", {})
    happy_response = (happy_day_example or {}).get("response", {})
    return f"""
    <div class="exhibit">
      <h3>API schema</h3>
      <pre class="schema-doc">{esc(api_schema)}</pre>
    </div>
    <div class="exhibit">
      <h3>Happy-day example</h3>
      <p class="eyebrow">Request</p>
      {render_json_block(happy_request.get('body', {}))}
      <p class="eyebrow">Response</p>
      {render_json_block(happy_response.get('body', {}))}
    </div>
    """


ADAPTER = SUTAdapter(
    name="complex_sut",
    display_name="Complex-SUT",
    base_url=BASE_URL,
    test_endpoint_path=TEST_ENDPOINT_PATH,
    api_schema_doc=API_SCHEMA_DOC,
    onboarding_extra={},
    happy_day_request=HAPPY_DAY_REQUEST,
    casting_tool_schema=CASTING_TOOL,
    casting_system_prompt=casting_system_prompt,
    validate_casting_response=validate_casting_response,
    casting_max_tokens=lambda budget: 3072 if budget <= 6 else 5120,
    execute_test=execute_test,
    describe_test_for_log=describe_test_for_log,
    describe_result_for_log=describe_result_for_log,
    render_test_entry=render_test_entry,
    render_onboarding_section=render_onboarding_section,
    default_max_checkpoints=4,
    default_first_round_test_budget=10,
    default_test_budget=6,
)
