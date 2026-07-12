"""Phase 4 of the adapter-bootstrap roadmap: the handoff. Turns a
BootstrapResult (from engine.bootstrap.probe) into an actual, runnable
SUTAdapter Python file - closing the loop from "point this at a live API"
to "now bug-hunt it with the existing checkpoint loop," with zero
hand-written adapter code for the simple case.

Scoped conservatively, as agreed from the start of this roadmap: only the
"one request in, one response out, compare a configurable success signal"
execute_test pattern is auto-generatable this way - complex_sut-style
concurrency/aggregation logic stays human-authored. Output is a draft for
a human to review - never auto-registered in engine/adapters/registry.py,
never auto-run unattended.
"""

import pprint
from pathlib import Path

from engine.bootstrap.probe import BootstrapResult

_GENERIC_CASTING_SYSTEM_PROMPT_BODY = '''You are testing a live API endpoint ({method} {path}) to look for
bugs or unexpected behavior. You've been shown the API's schema documentation and one real executed
"happy day" example.

{context_instruction}

In one round, propose a BATCH of tests - up to {{test_budget}} total:
1. Candidate hypotheses: think of a few specific, falsifiable theories about possible bugs - e.g.
   boundary values, type coercion issues, validation ordering that leaks information before
   authorization is confirmed, off-by-one errors, or inconsistent handling of missing vs. null vs.
   empty values. For each, propose 1-2 concrete test ideas - a full request plus a prediction of what
   would happen if that specific theory were true. Set linked_hypothesis to the full theory text.
2. Pure edge-case probes: also propose tests not tied to any specific theory - general negative-case/
   boundary testing instinct. Predict "2xx" as your null hypothesis and set linked_hypothesis to an
   empty string.

All of these tests will be executed for real, together, before you see any results. If you believe
you've explored reasonably and have no more good ideas worth proposing, set give_up to true rather
than proposing something arbitrary just to have something to submit.

Prioritize breadth over depth. Before proposing a test, check tests_tried_in_earlier_rounds: if the
same underlying question has already been asked multiple times with consistent results, treat it as
settled.

Call submit_casting_round with your answer.'''

_FIRST_ROUND_CONTEXT = """Nothing is currently flagged as anomalous, and you do not know whether any bug
exists at all - this adapter was auto-generated from a discovered/confirmed schema, not hand-audited.
Before proposing anything, think about common bug classes for REST endpoints in general: boundary
values, type coercion issues, validation ordering that leaks information before authorization is
confirmed, off-by-one errors at numeric limits, and inconsistent handling of missing vs. null vs. empty
values. State this reasoning explicitly."""

_LATER_ROUND_CONTEXT = """You now have real test results, and prior_checkpoint_feedback holds the
previous checkpoint's hypothesis plus Skeptic's cold critique of it. If that hypothesis claimed any
anomalies that Skeptic found weak, prioritize tests that could confirm OR refute those SPECIFIC claims.
Briefly state what you've actually learned so far and how that's changing your approach this round."""


def _field_json_schema_property(field) -> dict:
    prop = {"type": field.type, "description": field.description}
    if field.enum:
        prop["enum"] = field.enum
    return prop


def _build_casting_tool(endpoint) -> dict:
    item_properties = {"linked_hypothesis": {"type": "string"}}
    required = ["linked_hypothesis"]
    for f in endpoint.request_fields:
        item_properties[f.name] = _field_json_schema_property(f)
        if f.required:
            required.append(f.name)
    item_properties["predicted_outcome"] = {"type": "string", "description": "What you predict will happen and why."}
    item_properties["predicted_status_family"] = {
        "type": "string",
        "enum": ["2xx", "4xx", "5xx"],
        "description": "The broad class of HTTP status you predict this test will get back.",
    }
    required += ["predicted_outcome", "predicted_status_family"]

    return {
        "name": "submit_casting_round",
        "description": f"Propose a batch of tests against the live {endpoint.path} endpoint.",
        "input_schema": {
            "type": "object",
            "properties": {
                "give_up": {
                    "type": "boolean",
                    "description": "Set true only if you have no more good ideas worth proposing this round.",
                },
                "reasoning": {"type": "string", "description": "Your reasoning for this round's batch."},
                "candidate_tests": {
                    "type": "array",
                    "description": (
                        "Each is EITHER tied to a specific candidate hypothesis (set linked_hypothesis to "
                        "that theory) OR a pure edge-case probe not tied to any theory (empty string)."
                    ),
                    "items": {"type": "object", "properties": item_properties, "required": required},
                },
            },
            "required": ["give_up", "reasoning", "candidate_tests"],
        },
    }


def _as_comment_block(text: str) -> str:
    """Prefixes every line of arbitrary (possibly multi-line, LLM-authored)
    text with '# ' so it's always a syntactically valid comment block,
    regardless of what the text contains."""
    return "\n".join(f"# {line}" for line in text.splitlines()) or "# "


def _render_api_schema_doc(endpoint) -> str:
    lines = [f"{endpoint.method} {endpoint.path}", "", "Request body:"]
    for f in endpoint.request_fields:
        req = "required" if f.required else "optional"
        enum_note = f" (one of {f.enum})" if f.enum else ""
        desc = f" - {f.description}" if f.description else ""
        lines.append(f"  {f.name}: {f.type} - {req}{enum_note}{desc}")
    lines.append("")
    lines.append(
        "This schema was discovered/confirmed by the adapter-bootstrap tool, not hand-written - "
        "review it before trusting it fully."
    )
    return "\n".join(lines)


def _render_validator_source(endpoint) -> str:
    field_metadata = [
        {"name": f.name, "type": f.type, "required": f.required, "enum": f.enum}
        for f in endpoint.request_fields
    ]
    metadata_literal = pprint.pformat(field_metadata, sort_dicts=False, width=100)
    field_names = [f.name for f in endpoint.request_fields]
    required_keys_literal = pprint.pformat(
        ["linked_hypothesis", *field_names, "predicted_outcome", "predicted_status_family"],
        sort_dicts=False, width=100,
    )

    return f'''_JSON_TYPE_TO_PYTHON = {{
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict,
}}

_DISCOVERED_FIELDS = {metadata_literal}


def validate_casting_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {{type(data).__name__}}"]

    for key in ("give_up", "reasoning", "candidate_tests"):
        if key not in data:
            errors.append(f"missing required field '{{key}}'")

    if not isinstance(data.get("give_up"), bool):
        errors.append("'give_up' must be a boolean")

    tests = data.get("candidate_tests")
    if not isinstance(tests, list):
        errors.append("'candidate_tests' must be a list")
    elif not data.get("give_up") and not tests:
        errors.append("'candidate_tests' must be non-empty unless give_up is true")
    else:
        required_keys = {required_keys_literal}
        for i, test in enumerate(tests or []):
            if not isinstance(test, dict):
                errors.append(f"candidate_tests[{{i}}] must be an object")
                continue
            for key in required_keys:
                if key not in test:
                    errors.append(f"candidate_tests[{{i}}] missing '{{key}}'")
            for field_meta in _DISCOVERED_FIELDS:
                name = field_meta["name"]
                if name not in test:
                    continue
                expected_type = _JSON_TYPE_TO_PYTHON.get(field_meta["type"])
                if expected_type is not None and not isinstance(test[name], expected_type):
                    errors.append(f"candidate_tests[{{i}}].{{name}} must be of type {{field_meta['type']}}")
                if field_meta["enum"] and test[name] not in field_meta["enum"]:
                    errors.append(f"candidate_tests[{{i}}].{{name}} must be one of {{field_meta['enum']}}")
            if test.get("predicted_status_family") not in ("2xx", "4xx", "5xx"):
                errors.append(f"candidate_tests[{{i}}].predicted_status_family must be one of ('2xx', '4xx', '5xx')")

    return errors'''


def generate_adapter_source(name: str, display_name: str, base_url: str, bootstrap_result: BootstrapResult) -> str:
    if bootstrap_result.status == "failed":
        raise ValueError(
            "Cannot generate an adapter from a failed bootstrap result - no confirmed working "
            "example was ever achieved, so there's nothing real to build around."
        )
    if not bootstrap_result.happy_day_example:
        raise ValueError("bootstrap_result has no happy_day_example - cannot generate a HAPPY_DAY_REQUEST from it.")

    endpoint = bootstrap_result.schema.endpoints[0]
    casting_tool = _build_casting_tool(endpoint)
    casting_tool_literal = pprint.pformat(casting_tool, sort_dicts=False, width=100)
    api_schema_doc = _render_api_schema_doc(endpoint)
    happy_day_request = bootstrap_result.happy_day_example["request"]["body"]
    happy_day_request_literal = pprint.pformat(happy_day_request, sort_dicts=False, width=100)
    request_field_names_literal = pprint.pformat(
        [f.name for f in endpoint.request_fields], sort_dicts=False, width=100
    )
    validator_source = _render_validator_source(endpoint)

    draft_warning = ""
    if bootstrap_result.status == "inconclusive":
        notes_comment = _as_comment_block(bootstrap_result.notes or "(no further detail recorded)")
        draft_warning = f'''
# ============================================================================
# DRAFT, NOT FULLY CONFIRMED: the adapter-bootstrap probing loop ran out of
# budget before reaching confidence. Known remaining gaps, in its own words:
#
{notes_comment}
#
# Review this file carefully before trusting it - especially execute_test()
# and the discovered field list below - before running real bug-hunting
# checkpoints against it.
# ============================================================================
'''

    prompt_body = _GENERIC_CASTING_SYSTEM_PROMPT_BODY.format(
        method=endpoint.method, path=endpoint.path, context_instruction="{context_instruction}"
    )

    return f'''"""Auto-generated by engine.bootstrap.generate (adapter-bootstrap Phase 4)
for "{display_name}" - a DRAFT for human review, not hand-audited. Bootstrap
status: {bootstrap_result.status}.
"""
{draft_warning}
from engine.adapter import SUTAdapter
from engine.http import call_sut_once
from engine.report import bool_badge, esc, inline_markdown, render_json_block

BASE_URL = {base_url!r}
TEST_ENDPOINT_PATH = {endpoint.path!r}
HTTP_METHOD = {endpoint.method!r}

API_SCHEMA_DOC = {api_schema_doc!r}

HAPPY_DAY_REQUEST = {happy_day_request_literal}

_REQUEST_FIELD_NAMES = {request_field_names_literal}


def execute_test(test: dict, test_number: int) -> dict:
    request_body = {{k: v for k, v in test.items() if k in _REQUEST_FIELD_NAMES}}
    request = {{"method": HTTP_METHOD, "path": TEST_ENDPOINT_PATH, "body": request_body}}

    response = call_sut_once(BASE_URL, TEST_ENDPOINT_PATH, request_body, method=HTTP_METHOD)
    actual_status_family = f"{{response['status'] // 100}}xx"
    predicted_status_family = test["predicted_status_family"]
    prediction_matched = actual_status_family == predicted_status_family

    return {{
        "test_number": test_number,
        "request": request,
        "response": response,
        "predicted_outcome": test["predicted_outcome"],
        "predicted_status_family": predicted_status_family,
        "actual_status_family": actual_status_family,
        "prediction_matched": prediction_matched,
    }}


CASTING_TOOL = {casting_tool_literal}

_FIRST_ROUND_CONTEXT = """{_FIRST_ROUND_CONTEXT}"""

_LATER_ROUND_CONTEXT = """{_LATER_ROUND_CONTEXT}"""


def casting_system_prompt(test_budget: int, is_first_round: bool) -> str:
    if is_first_round:
        context_instruction = _FIRST_ROUND_CONTEXT
    else:
        context_instruction = _LATER_ROUND_CONTEXT

    return f"""{prompt_body}"""


{validator_source}


def render_test_entry(entry) -> str:
    if not entry:
        return ""
    request = entry.get("request", {{}})
    body = request.get("body", {{}})

    linked = entry.get("linked_hypothesis")
    linked_html = f"<strong>Hypothesis:</strong> {{esc(linked)}}" if linked else (
        '<span class="probe-label">Edge-case probe</span> (no linked hypothesis)'
    )
    test_number = entry.get("test_number")
    number_html = f'<span class="test-number">Test #{{esc(test_number)}}</span>' if test_number is not None else ""

    predicted = entry.get("predicted_status_family")
    actual = entry.get("actual_status_family")
    matched = entry.get("prediction_matched")

    return f"""
    <article class="test">
      <div class="test-hypothesis">{{number_html}}{{linked_html}}</div>
      {{render_json_block(body)}}
      <div class="test-predicted">Predicted: {{inline_markdown(entry.get('predicted_outcome'))}}
        <span class="prose-muted">{{esc(predicted)}}</span></div>
      <div class="test-outcome">
        Actual: <span class="prose-muted">{{esc(actual)}}</span>
        <span class="sep">&middot;</span> prediction {{bool_badge(matched, 'matched', 'missed')}}
      </div>
    </article>
    """


def render_onboarding_section(api_schema, onboarding_extra, happy_day_example) -> str:
    happy_request = (happy_day_example or {{}}).get("request", {{}})
    happy_response = (happy_day_example or {{}}).get("response", {{}})
    return f"""
    <div class="exhibit">
      <h3>API schema</h3>
      <pre class="schema-doc">{{esc(api_schema)}}</pre>
    </div>
    <div class="exhibit">
      <h3>Happy-day example</h3>
      <p class="eyebrow">Request</p>
      {{render_json_block(happy_request.get('body', {{}}))}}
      <p class="eyebrow">Response</p>
      {{render_json_block(happy_response.get('body', {{}}))}}
    </div>
    """


ADAPTER = SUTAdapter(
    name={name!r},
    display_name={display_name!r},
    base_url=BASE_URL,
    test_endpoint_path=TEST_ENDPOINT_PATH,
    api_schema_doc=API_SCHEMA_DOC,
    happy_day_request=HAPPY_DAY_REQUEST,
    casting_tool_schema=CASTING_TOOL,
    casting_system_prompt=casting_system_prompt,
    validate_casting_response=validate_casting_response,
    execute_test=execute_test,
    render_test_entry=render_test_entry,
    render_onboarding_section=render_onboarding_section,
)
'''


def write_adapter_module(output_dir: Path, name: str, source: str) -> Path:
    module_dir = output_dir / name
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    adapter_path = module_dir / "adapter.py"
    adapter_path.write_text(source, encoding="utf-8")
    return adapter_path
