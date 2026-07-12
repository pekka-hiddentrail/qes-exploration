"""Phase 2 of the adapter-bootstrap roadmap: free-text interpretation, used
only as a fallback when Phase 1 (engine.bootstrap.discovery) finds no formal
OpenAPI/Swagger schema. One tool-forced LLM call reads whatever free text
describes the API and proposes a draft schema - explicitly a draft, not a
confirmed fact, since nothing here has been checked against the real system
yet. Follows the same tool/system-prompt/validator pattern as every other
LLM call in this project (see engine/tools.py).
"""

from engine.bootstrap.discovery import DiscoveredEndpoint, DiscoveredSchema, field_from_dict
from engine.client import DEFAULT_MAX_ATTEMPTS, DEFAULT_MODEL, call_tool_with_retry

_JSON_SCHEMA_TYPES = ("string", "integer", "number", "boolean", "array", "object", "unknown")

_FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "type": {"type": "string", "enum": list(_JSON_SCHEMA_TYPES)},
        "required": {"type": "boolean"},
        "enum": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Empty list if unconstrained - don't invent plausible-looking values the text doesn't state.",
        },
        "description": {"type": "string"},
    },
    "required": ["name", "type", "required", "enum", "description"],
}

FREETEXT_SCHEMA_TOOL = {
    "name": "submit_schema_draft",
    "description": (
        "Propose a draft API schema based on free-text documentation - this is unconfirmed, "
        "not yet verified against the real system."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "endpoint_path": {
                "type": "string",
                "description": "Path of the endpoint, e.g. /submit",
            },
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]},
            "request_fields": {
                "type": "array",
                "description": "Fields the request body appears to need, based on the text.",
                "items": _FIELD_SCHEMA,
            },
            "response_fields": {
                "type": "array",
                "description": (
                    "Fields the response appears to include, if the text says anything about it. "
                    "Empty list if the text doesn't describe the response at all."
                ),
                "items": _FIELD_SCHEMA,
            },
            "confidence_notes": {
                "type": "string",
                "description": (
                    "Honest notes on what you're unsure about - fields you had to guess at rather than "
                    "read explicitly, ambiguous wording, anything the text simply didn't say. This "
                    "schema is a draft that still needs to be confirmed against the real system."
                ),
            },
        },
        "required": ["endpoint_path", "method", "request_fields", "response_fields", "confidence_notes"],
    },
}

FREETEXT_SCHEMA_SYSTEM_PROMPT = """You are reading free-text documentation for an API endpoint - no
formal OpenAPI/Swagger schema was found for this system, so this is the fallback: propose a structured
draft schema from whatever the text actually says.

This is explicitly a DRAFT, not a confirmed fact - you have not seen the real system respond to
anything. Read the text carefully and distinguish between what it states outright (e.g. "auth_token is
a string") and what you're inferring or guessing (e.g. assuming a field is required because the text
doesn't say either way). Use confidence_notes to be honest about which fields you had to guess at,
where the text was ambiguous, and what it simply never mentioned - a later stage will actively test
this draft against the real system to confirm or correct it, so understating your uncertainty here
just means that stage starts from a worse position.

Do not invent fields the text gives you no basis for. If the text only mentions one field, propose one
field - do not pad the schema with plausible-sounding fields you made up to seem thorough.

Call submit_schema_draft with your answer."""


def validate_schema_draft_response(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    for key in ("endpoint_path", "method", "request_fields", "response_fields", "confidence_notes"):
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if "endpoint_path" in data and not isinstance(data.get("endpoint_path"), str):
        errors.append("'endpoint_path' must be a string")
    if "confidence_notes" in data and not isinstance(data.get("confidence_notes"), str):
        errors.append("'confidence_notes' must be a string")
    if data.get("method") not in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
        errors.append("'method' must be one of GET/POST/PUT/PATCH/DELETE/OPTIONS/HEAD")
    for list_key in ("request_fields", "response_fields"):
        fields = data.get(list_key)
        if not isinstance(fields, list):
            errors.append(f"'{list_key}' must be a list")
            continue
        for i, item in enumerate(fields):
            if not isinstance(item, dict):
                errors.append(f"{list_key}[{i}] must be an object")
                continue
            for key in ("name", "type", "required", "enum", "description"):
                if key not in item:
                    errors.append(f"{list_key}[{i}] missing '{key}'")
            if not isinstance(item.get("name"), str):
                errors.append(f"{list_key}[{i}].name must be a string")
            if item.get("type") not in _JSON_SCHEMA_TYPES:
                errors.append(f"{list_key}[{i}].type must be a valid JSON Schema type")
            if not isinstance(item.get("required"), bool):
                errors.append(f"{list_key}[{i}].required must be a boolean")
            enum = item.get("enum")
            if not isinstance(enum, list) or not all(isinstance(v, str) for v in enum):
                errors.append(f"{list_key}[{i}].enum must be a list of strings")
            if not isinstance(item.get("description"), str):
                errors.append(f"{list_key}[{i}].description must be a string")

    return errors


def propose_schema_from_text(
    client, spec_text: str, model: str = DEFAULT_MODEL, max_attempts: int = DEFAULT_MAX_ATTEMPTS
) -> DiscoveredSchema:
    result = call_tool_with_retry(
        client,
        model=model,
        system=FREETEXT_SCHEMA_SYSTEM_PROMPT,
        tools=[FREETEXT_SCHEMA_TOOL],
        tool_name="submit_schema_draft",
        user_message=spec_text,
        validate_fn=validate_schema_draft_response,
        max_tokens=2048,
        max_attempts=max_attempts,
    )

    endpoint = DiscoveredEndpoint(
        path=result["endpoint_path"],
        method=result["method"],
        request_fields=[field_from_dict(f) for f in result["request_fields"]],
        response_fields=[field_from_dict(f) for f in result["response_fields"]],
        raw_request_schema={},
        raw_response_schema={},
    )
    return DiscoveredSchema(
        status="found",
        fetched_from=None,
        endpoints=[endpoint],
        source="freetext",
        confirmed=False,
        notes=result["confidence_notes"],
    )
