"""call_tool_with_retry's validate-fail -> retry-with-feedback -> RuntimeError
path, plus transient-vs-permanent API error handling, checked against a
stubbed Anthropic client - no network calls."""

import httpx
import pytest

import anthropic
from engine.client import call_tool_with_retry


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
        self.call_count = 0

    def create(self, **kwargs):
        self.call_count += 1
        item = next(self._responses)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)


def _connection_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def _rate_limit_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)


def _auth_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError("invalid api key", response=httpx.Response(401, request=request), body=None)


def test_retry_succeeds_after_one_validation_failure():
    responses = [
        _FakeMessage([_FakeToolUse("id1", {"bad": True})]),
        _FakeMessage([_FakeToolUse("id2", {"ok": True})]),
    ]
    client = _FakeClient(responses)

    def validate(data):
        return [] if data.get("ok") else ["missing 'ok'"]

    result = call_tool_with_retry(
        client, model="m", system="s", tools=[], tool_name="t", user_message="u",
        validate_fn=validate, max_tokens=10, max_attempts=3,
    )
    assert result == {"ok": True}


def test_raises_after_max_attempts_exhausted():
    responses = [_FakeMessage([_FakeToolUse(f"id{i}", {"bad": True})]) for i in range(5)]
    client = _FakeClient(responses)

    def validate(data):
        return ["always fails"]

    with pytest.raises(RuntimeError, match="Gave up after 2 attempts"):
        call_tool_with_retry(
            client, model="m", system="s", tools=[], tool_name="t", user_message="u",
            validate_fn=validate, max_tokens=10, max_attempts=2,
        )


def test_retries_when_no_tool_use_block_returned():
    responses = [
        _FakeMessage([], stop_reason="end_turn"),
        _FakeMessage([_FakeToolUse("id2", {"ok": True})]),
    ]
    client = _FakeClient(responses)

    def validate(data):
        return [] if data.get("ok") else ["missing 'ok'"]

    result = call_tool_with_retry(
        client, model="m", system="s", tools=[], tool_name="t", user_message="u",
        validate_fn=validate, max_tokens=10, max_attempts=3,
    )
    assert result == {"ok": True}


def test_retries_on_transient_connection_error(monkeypatch):
    monkeypatch.setattr("engine.client.time.sleep", lambda s: None)
    responses = [_connection_error(), _FakeMessage([_FakeToolUse("id1", {"ok": True})])]
    client = _FakeClient(responses)

    result = call_tool_with_retry(
        client, model="m", system="s", tools=[], tool_name="t", user_message="u",
        validate_fn=lambda d: [], max_tokens=10, max_attempts=3,
    )
    assert result == {"ok": True}
    assert client.messages.call_count == 2


def test_retries_on_rate_limit_error(monkeypatch):
    monkeypatch.setattr("engine.client.time.sleep", lambda s: None)
    responses = [_rate_limit_error(), _FakeMessage([_FakeToolUse("id1", {"ok": True})])]
    client = _FakeClient(responses)

    result = call_tool_with_retry(
        client, model="m", system="s", tools=[], tool_name="t", user_message="u",
        validate_fn=lambda d: [], max_tokens=10, max_attempts=3,
    )
    assert result == {"ok": True}
    assert client.messages.call_count == 2


def test_raises_runtime_error_after_max_attempts_of_transient_errors(monkeypatch):
    monkeypatch.setattr("engine.client.time.sleep", lambda s: None)
    responses = [_connection_error(), _rate_limit_error(), _connection_error()]
    client = _FakeClient(responses)

    with pytest.raises(RuntimeError, match="Gave up after 3 attempts"):
        call_tool_with_retry(
            client, model="m", system="s", tools=[], tool_name="t", user_message="u",
            validate_fn=lambda d: [], max_tokens=10, max_attempts=3,
        )
    assert client.messages.call_count == 3


def test_non_retryable_error_propagates_immediately_without_retrying(monkeypatch):
    monkeypatch.setattr(
        "engine.client.time.sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("should not sleep/retry")),
    )
    responses = [_auth_error(), _FakeMessage([_FakeToolUse("id1", {"ok": True})])]
    client = _FakeClient(responses)

    with pytest.raises(anthropic.AuthenticationError):
        call_tool_with_retry(
            client, model="m", system="s", tools=[], tool_name="t", user_message="u",
            validate_fn=lambda d: [], max_tokens=10, max_attempts=3,
        )
    # A permanent error (bad API key) shouldn't burn attempt budget retrying -
    # only the first, failing call should have happened.
    assert client.messages.call_count == 1
