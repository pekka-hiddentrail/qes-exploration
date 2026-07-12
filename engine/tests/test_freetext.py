"""engine.bootstrap.freetext.propose_schema_from_text - Phase 2 of the
adapter-bootstrap roadmap, only used when Phase 1 finds no formal schema.
Checked against a stubbed Anthropic client (same pattern as
test_client_retry.py) - no real LLM calls. The retry/validation-failure
mechanics themselves are already covered there; these tests focus on
what's actually new here: converting a tool-call response into
DiscoveredSchema/DiscoveredEndpoint/DiscoveredField, and marking the
result as an unconfirmed draft."""

from engine.bootstrap.discovery import DiscoveredSchema
from engine.bootstrap.freetext import propose_schema_from_text, validate_schema_draft_response


class _FakeToolUse:
    def __init__(self, id, input):
        self.type = "tool_use"
        self.id = id
        self.input = input


class _FakeMessage:
    def __init__(self, content, stop_reason="tool_use"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = iter(responses)

    def create(self, **kwargs):
        return next(self._responses)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)


def _canned_response(**overrides):
    base = {
        "endpoint_path": "/submit",
        "method": "POST",
        "request_fields": [
            {"name": "client_id", "type": "string", "required": True, "enum": [], "description": "the caller's id"},
            {"name": "priority", "type": "string", "required": False, "enum": ["normal", "high"], "description": ""},
        ],
        "response_fields": [],
        "confidence_notes": "Guessed that priority is optional since the text didn't say either way.",
    }
    base.update(overrides)
    return base


def test_propose_schema_from_text_produces_an_unconfirmed_freetext_draft():
    client = _FakeClient([_FakeMessage([_FakeToolUse("id1", _canned_response())])])

    result = propose_schema_from_text(
        client, "POST /submit takes a client_id (string) and an optional priority (normal or high)."
    )

    assert isinstance(result, DiscoveredSchema)
    assert result.status == "found"
    assert result.source == "freetext"
    assert result.confirmed is False
    assert result.notes == "Guessed that priority is optional since the text didn't say either way."

    [endpoint] = result.endpoints
    assert endpoint.path == "/submit"
    assert endpoint.method == "POST"

    fields_by_name = {f.name: f for f in endpoint.request_fields}
    assert fields_by_name["client_id"].required is True
    assert fields_by_name["client_id"].enum is None
    assert fields_by_name["priority"].required is False
    assert fields_by_name["priority"].enum == ["normal", "high"]


def test_propose_schema_from_text_empty_enum_becomes_none_not_empty_list():
    client = _FakeClient([_FakeMessage([_FakeToolUse("id1", _canned_response())])])
    result = propose_schema_from_text(client, "some text")
    client_id_field = next(f for f in result.endpoints[0].request_fields if f.name == "client_id")
    # DiscoveredField's own convention: enum=None means unconstrained, not
    # an empty list - the same convention Phase 1's parser already uses.
    assert client_id_field.enum is None


def test_propose_schema_from_text_response_fields_can_be_empty():
    client = _FakeClient([_FakeMessage([_FakeToolUse("id1", _canned_response())])])
    result = propose_schema_from_text(client, "some text")
    assert result.endpoints[0].response_fields == []


# --- validator ---

def test_validate_schema_draft_response_accepts_well_formed_data():
    assert validate_schema_draft_response(_canned_response()) == []


def test_validate_schema_draft_response_rejects_bad_method():
    errors = validate_schema_draft_response(_canned_response(method="FETCH"))
    assert any("method" in e for e in errors)


def test_validate_schema_draft_response_rejects_missing_field_keys():
    bad = _canned_response(request_fields=[{"name": "x"}])  # missing type/required/enum/description
    errors = validate_schema_draft_response(bad)
    assert any("request_fields[0]" in e for e in errors)


def test_validate_schema_draft_response_rejects_invalid_field_type():
    bad = _canned_response(request_fields=[
        {"name": "x", "type": "not-a-real-type", "required": True, "enum": [], "description": ""},
    ])
    errors = validate_schema_draft_response(bad)
    assert any("type must be a valid JSON Schema type" in e for e in errors)
