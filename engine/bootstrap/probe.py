"""Phase 3 of the adapter-bootstrap roadmap: the active probing loop. Given
a schema (confirmed from Phase 1, or an unconfirmed draft from Phase 2),
actively sends real requests to the live SUT to confirm or correct it and
find at least one genuine working example - not by guessing, but by reading
how the real system actually responds. Real error messages are the richest
signal available: a 422 saying "missing required field: cvv" resolves an
unknown more reliably than a successful response would.

Two tool-forced LLM calls per round (propose one probe, then review
everything so far and decide whether to keep going) - not three. The
Driver/Skeptic split in engine/loop.py exists because it's a genuine
adversarial-perspective split (the Skeptic reviews cold); "update the
schema" and "decide whether to stop" here are one coherent judgment from
one voice, not a different perspective, so splitting them would just be
procedural fragmentation at ~50% more LLM cost per round for no real gain.

Deliberately leaner than the bug-hunting SKEPTIC_TOOL (engine/tools.py),
which grew its 8 fields (inference_validity_check, coverage_breadth_check,
prior_critique_addressed, ...) incrementally in direct response to specific
observed failures in that domain. This reviewer starts simple; harden it
later if a real problem shows up here too, not speculatively now.
"""

import json
from dataclasses import dataclass, field

import httpx

from engine.bootstrap.discovery import DiscoveredEndpoint, DiscoveredSchema, field_from_dict
from engine.client import DEFAULT_MAX_ATTEMPTS, DEFAULT_MODEL, call_tool_with_retry
from engine.http import call_sut_once

_JSON_SCHEMA_TYPES = ("string", "integer", "number", "boolean", "array", "object", "unknown")
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@dataclass(frozen=True)
class BootstrapResult:
    status: str  # "confirmed" | "inconclusive" | "failed"
    schema: DiscoveredSchema  # confirmed=True only if status == "confirmed"
    probe_log: list[dict] = field(default_factory=list)  # every probe: {request, response, reasoning}
    happy_day_example: dict | None = None  # the FIRST successful probe's request/response, if any
    notes: str = ""


PROBE_TOOL = {
    "name": "submit_probe_request",
    "description": (
        "Propose ONE concrete HTTP request to send to the live SUT, designed to resolve a specific "
        "remaining unknown about its schema."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "give_up": {
                "type": "boolean",
                "description": "Set true only if you have no more good probes worth trying, or the schema is already well-confirmed.",
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "What specific unknown this probe is trying to resolve, and what response - success "
                    "or a specific error - would be informative."
                ),
            },
            "method": {"type": "string", "enum": list(_HTTP_METHODS)},
            "path": {"type": "string", "description": "The endpoint path to send the request to, e.g. '/submit'."},
            "body": {
                "type": "object",
                "description": (
                    "The request body to send, using your current best understanding of the schema - "
                    "fill in every field you currently believe exists, with a real, plausible value for "
                    "each, not a placeholder."
                ),
            },
        },
        "required": ["give_up", "reasoning", "method", "path", "body"],
    },
}

PROBE_SYSTEM_PROMPT = """You are actively confirming a draft API schema against the real, live system -
not guessing from documentation, but finding out how it actually behaves. You've been given the current
best-understanding of the schema (which fields exist, their types, whether required) and the full log
of every probe tried so far, with the real request sent and the real response received.

Propose ONE concrete request to send next - a specific method, path, and body - designed to resolve a
SPECIFIC remaining unknown. Real error responses are often the richest signal available: a 422 saying
"missing required field: cvv" or "priority must be one of ['normal', 'high']" tells you exactly what
you needed to know, more reliably than a successful response would. If an earlier probe's response
included an error message you haven't yet acted on, prioritize a probe that follows up on it directly
rather than moving on to something unrelated.

Fill in the body using your current best understanding of every field you believe exists - if you're
unsure about a field's exact type or a valid value, that uncertainty is exactly what this probe should
be designed to resolve.

If you believe you've achieved a genuine, confirmed success and the remaining unknowns are minor, or
you have no more good probes worth trying, set give_up to true rather than sending something arbitrary
just to have something to submit.

Call submit_probe_request with your answer."""


def validate_probe_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    for key in ("give_up", "reasoning", "method", "path", "body"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if not isinstance(data.get("give_up"), bool):
        errors.append("'give_up' must be a boolean")
    if not isinstance(data.get("reasoning"), str):
        errors.append("'reasoning' must be a string")
    if data.get("method") not in _HTTP_METHODS:
        errors.append(f"'method' must be one of {_HTTP_METHODS}")
    if not isinstance(data.get("path"), str):
        errors.append("'path' must be a string")
    if not isinstance(data.get("body"), dict):
        errors.append("'body' must be an object")
    return errors


_REVIEW_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": list(_JSON_SCHEMA_TYPES)},
        "required": {"type": "boolean"},
        "enum": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Empty list if unconstrained - only include values you've actually seen accepted or "
                "named in an error message, not invented ones."
            ),
        },
        "description": {"type": "string"},
    },
    "required": ["name", "type", "required", "enum", "description"],
}

REVIEW_TOOL = {
    "name": "submit_schema_review",
    "description": (
        "Given everything probed so far, provide the current best-understanding schema and assess "
        "whether it's confirmed enough to stop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["needs_more_probing", "confident_enough"],
                "description": (
                    "'confident_enough' only if at least one probe has genuinely succeeded AND the "
                    "remaining unknowns are minor. 'needs_more_probing' otherwise - never claim "
                    "confidence in a schema that has never actually produced a real success."
                ),
            },
            "updated_fields": {
                "type": "array",
                "description": (
                    "The FULL current best-understanding of the schema's fields, incorporating "
                    "everything learned from every probe so far - not just what changed this round."
                ),
                "items": _REVIEW_FIELD_SCHEMA,
            },
            "gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete fields or behaviors still unconfirmed.",
                "minItems": 2,
            },
            "recommended_next_probes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "At least 2 concrete probe ideas that would resolve the named gaps.",
                "minItems": 2,
            },
            "reasoning": {"type": "string"},
        },
        "required": ["verdict", "updated_fields", "gaps", "recommended_next_probes", "reasoning"],
    },
}

REVIEW_SYSTEM_PROMPT = """You are reviewing everything probed against a live system so far, to decide
whether the schema is now well-confirmed or more probing is still needed.

You cannot claim "confident_enough" if none of the probes so far received a genuine successful
response - a schema that has never actually worked cannot be considered confirmed, no matter how
well-reasoned the guesses behind it are. If there has been at least one real success, weigh whether the
remaining unknowns (fields whose type, requiredness, or valid values are still guessed rather than
confirmed by an actual response) are significant enough to keep probing, or minor enough to stop.

Provide updated_fields as your FULL current understanding of the schema - every field you now believe
exists, incorporating everything learned from every probe so far, not just what changed this round.
Identify at least 2 concrete gaps (fields or behaviors still unconfirmed) and at least 2 concrete next
probes that would resolve them, even if your verdict is confident_enough - a later stage may still want
them recorded as known remaining unknowns.

Call submit_schema_review with your answer."""


def validate_review_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]
    required = ("verdict", "updated_fields", "gaps", "recommended_next_probes", "reasoning")
    for key in required:
        if key not in data:
            errors.append(f"missing required field '{key}'")
    if data.get("verdict") not in ("needs_more_probing", "confident_enough"):
        errors.append("'verdict' must be 'needs_more_probing' or 'confident_enough'")

    fields = data.get("updated_fields")
    if not isinstance(fields, list):
        errors.append("'updated_fields' must be a list")
    else:
        for i, item in enumerate(fields):
            if not isinstance(item, dict):
                errors.append(f"updated_fields[{i}] must be an object")
                continue
            for key in ("name", "type", "required", "enum", "description"):
                if key not in item:
                    errors.append(f"updated_fields[{i}] missing '{key}'")
            if item.get("type") not in _JSON_SCHEMA_TYPES:
                errors.append(f"updated_fields[{i}].type must be a valid JSON Schema type")
            if not isinstance(item.get("required"), bool):
                errors.append(f"updated_fields[{i}].required must be a boolean")
            if not isinstance(item.get("enum"), list):
                errors.append(f"updated_fields[{i}].enum must be a list")

    gaps = data.get("gaps")
    if not isinstance(gaps, list) or len(gaps) < 2:
        errors.append("'gaps' must be a list of at least 2 strings")
    next_probes = data.get("recommended_next_probes")
    if not isinstance(next_probes, list) or len(next_probes) < 2:
        errors.append("'recommended_next_probes' must be a list of at least 2 strings")

    return errors


def _get_probe(
    client, request_fields, probe_log: list[dict], prior_review: dict | None, model: str, max_attempts: int
) -> dict:
    evidence = {
        "current_schema_fields": [
            {"name": f.name, "type": f.type, "required": f.required, "enum": f.enum, "description": f.description}
            for f in request_fields
        ],
        "probes_so_far": probe_log,
    }
    if prior_review is not None:
        evidence["prior_review"] = prior_review
    return call_tool_with_retry(
        client,
        model=model,
        system=PROBE_SYSTEM_PROMPT,
        tools=[PROBE_TOOL],
        tool_name="submit_probe_request",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_probe_response,
        max_tokens=1536,
        max_attempts=max_attempts,
    )


def _get_review(client, probe_log: list[dict], model: str, max_attempts: int) -> dict:
    evidence = {"probes_so_far": probe_log}
    return call_tool_with_retry(
        client,
        model=model,
        system=REVIEW_SYSTEM_PROMPT,
        tools=[REVIEW_TOOL],
        tool_name="submit_schema_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_review_response,
        max_tokens=2048,
        max_attempts=max_attempts,
    )


def _build_endpoint(base: DiscoveredEndpoint, request_fields) -> DiscoveredEndpoint:
    return DiscoveredEndpoint(
        path=base.path,
        method=base.method,
        request_fields=request_fields,
        response_fields=base.response_fields,
        raw_request_schema={},
        raw_response_schema={},
    )


def run_bootstrap_probe_loop(
    anthropic_client,
    base_url: str,
    initial_schema: DiscoveredSchema,
    max_probes: int = 8,
    transport: httpx.BaseTransport | None = None,
    model: str = DEFAULT_MODEL,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    on_probe=None,
) -> BootstrapResult:
    """on_probe(probe_log), if given, is called after every round - not just
    once at the end - mirroring run_checkpoint_loop's on_checkpoint pattern,
    so a crash partway through doesn't discard probes that already ran."""
    endpoint = initial_schema.endpoints[0]
    probe_log: list[dict] = []
    happy_day_example: dict | None = None
    had_success = False
    prior_review: dict | None = None
    latest_fields = endpoint.request_fields

    for _ in range(max_probes):
        probe = _get_probe(anthropic_client, latest_fields, probe_log, prior_review, model, max_attempts)
        if probe["give_up"]:
            break

        response = call_sut_once(base_url, probe["path"], probe["body"], method=probe["method"], transport=transport)
        request = {"method": probe["method"], "path": probe["path"], "body": probe["body"]}
        probe_log.append({"request": request, "response": response, "reasoning": probe["reasoning"]})

        if response["status"] < 300 and not had_success:
            had_success = True
            happy_day_example = {"request": request, "response": response}

        review = _get_review(anthropic_client, probe_log, model, max_attempts)
        latest_fields = [field_from_dict(f) for f in review["updated_fields"]]

        # Code-level safety net: a claimed "confident_enough" with zero real
        # successes is not trustworthy, regardless of what the model said -
        # this is an objectively checkable fact, not something to leave to
        # the model's own self-assessment.
        verdict = review["verdict"]
        if verdict == "confident_enough" and not had_success:
            verdict = "needs_more_probing"

        prior_review = review
        if on_probe is not None:
            on_probe(list(probe_log))

        if verdict == "confident_enough":
            return BootstrapResult(
                status="confirmed",
                schema=DiscoveredSchema(
                    status="found", fetched_from=None, endpoints=[_build_endpoint(endpoint, latest_fields)],
                    source=initial_schema.source, confirmed=True, notes=review["reasoning"],
                ),
                probe_log=probe_log,
                happy_day_example=happy_day_example,
            )

    final_schema = DiscoveredSchema(
        status="found", fetched_from=None, endpoints=[_build_endpoint(endpoint, latest_fields)],
        source=initial_schema.source, confirmed=False,
    )
    if had_success:
        return BootstrapResult(status="inconclusive", schema=final_schema, probe_log=probe_log, happy_day_example=happy_day_example)
    return BootstrapResult(status="failed", schema=final_schema, probe_log=probe_log, happy_day_example=None)
