"""engine.bootstrap.schema.discover_or_draft_schema - ties Phase 1 (schema
discovery) and Phase 2 (free-text fallback) together. No real network or
LLM calls: discovery is exercised via a stubbed httpx client
(httpx.MockTransport, matching test_discovery.py's pattern), the free-text
fallback via a stubbed Anthropic client (matching test_freetext.py's
pattern). The two "must never fall through" tests use an empty response
list for whichever path shouldn't be reached - calling it raises
StopIteration, which surfaces as a test failure exactly as intended."""

import httpx

from engine.bootstrap.schema import discover_or_draft_schema


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


class _FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)


def _http_client_always_returning(status_code, json_body=None):
    def handler(request):
        if json_body is not None:
            return httpx.Response(status_code, json=json_body)
        return httpx.Response(status_code)
    return httpx.Client(transport=httpx.MockTransport(handler))


_VALID_OPENAPI_DOC = {
    "openapi": "3.1.0",
    "paths": {
        "/x": {
            "post": {
                "requestBody": {"content": {"application/json": {"schema": {"type": "object", "properties": {}}}}},
                "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
            },
        },
    },
    "components": {"schemas": {}},
}

_CANNED_FREETEXT_RESPONSE = {
    "endpoint_path": "/submit",
    "method": "POST",
    "request_fields": [],
    "response_fields": [],
    "confidence_notes": "n/a",
}


def test_returns_discovery_result_when_found_and_never_calls_the_llm():
    http_client = _http_client_always_returning(200, _VALID_OPENAPI_DOC)
    anthropic_client = _FakeAnthropicClient([])  # would raise StopIteration if ever called

    result = discover_or_draft_schema(
        "http://test", spec_text="irrelevant since discovery succeeds",
        anthropic_client=anthropic_client, http_client=http_client,
    )
    assert result.status == "found"
    assert result.source == "openapi"
    assert result.confirmed is True


def test_falls_back_to_freetext_when_discovery_finds_nothing():
    http_client = _http_client_always_returning(404)
    anthropic_client = _FakeAnthropicClient([_FakeMessage([_FakeToolUse("id1", _CANNED_FREETEXT_RESPONSE)])])

    result = discover_or_draft_schema(
        "http://test", spec_text="POST /submit with no fields",
        anthropic_client=anthropic_client, http_client=http_client,
    )
    assert result.source == "freetext"
    assert result.confirmed is False


def test_returns_discovery_failure_as_is_when_no_spec_text_given():
    http_client = _http_client_always_returning(404)
    anthropic_client = _FakeAnthropicClient([])  # would raise StopIteration if ever called

    result = discover_or_draft_schema(
        "http://test", spec_text=None, anthropic_client=anthropic_client, http_client=http_client,
    )
    assert result.status == "not_found"


def test_returns_discovery_failure_as_is_when_spec_text_is_empty_string():
    http_client = _http_client_always_returning(404)
    anthropic_client = _FakeAnthropicClient([])

    result = discover_or_draft_schema(
        "http://test", spec_text="", anthropic_client=anthropic_client, http_client=http_client,
    )
    assert result.status == "not_found"
