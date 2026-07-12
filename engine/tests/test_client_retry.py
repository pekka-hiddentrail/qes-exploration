"""call_tool_with_retry's validate-fail -> retry-with-feedback -> RuntimeError
path, checked against a stubbed Anthropic client - no network calls."""

import pytest

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

    def create(self, **kwargs):
        return next(self._responses)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)


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
