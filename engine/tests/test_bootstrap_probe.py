"""engine.bootstrap.probe.run_bootstrap_probe_loop - Phase 3 of the
adapter-bootstrap roadmap. No real network or LLM calls: the Anthropic
client is stubbed (same pattern as test_client_retry.py/test_freetext.py/
test_bootstrap_schema.py), the SUT via httpx.MockTransport."""

import httpx
import pytest

from engine.bootstrap.discovery import DiscoveredEndpoint, DiscoveredField, DiscoveredSchema, field_from_dict
from engine.bootstrap.freetext import field_from_dict as freetext_field_from_dict
from engine.bootstrap.probe import (
    BootstrapResult,
    field_from_dict as probe_field_from_dict,
    run_bootstrap_probe_loop,
    validate_probe_response,
    validate_review_response,
)


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
        return next(self._responses)


class _FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)


def _tool_response(tool_id, data):
    return _FakeMessage([_FakeToolUse(tool_id, data)])


def _canned_probe(give_up=False, method="POST", path="/submit", body=None, reasoning="testing an unknown"):
    return {
        "give_up": give_up, "reasoning": reasoning, "method": method, "path": path,
        "body": body if body is not None else {"client_id": "probe-client"},
    }


def _canned_review(verdict="needs_more_probing", updated_fields=None, reasoning="reviewing"):
    return {
        "verdict": verdict,
        "updated_fields": updated_fields if updated_fields is not None else [
            {"name": "client_id", "type": "string", "required": True, "enum": [], "description": ""},
        ],
        "gaps": ["gap one", "gap two"],
        "recommended_next_probes": ["probe idea one", "probe idea two"],
        "reasoning": reasoning,
    }


def _initial_schema() -> DiscoveredSchema:
    endpoint = DiscoveredEndpoint(
        path="/submit", method="POST",
        request_fields=[DiscoveredField(name="client_id", type="string", required=True)],
        response_fields=[], raw_request_schema={}, raw_response_schema={},
    )
    return DiscoveredSchema(status="found", fetched_from=None, endpoints=[endpoint], source="freetext", confirmed=False)


def _transport_always(status_code):
    return httpx.MockTransport(lambda request: httpx.Response(status_code, json={"ok": status_code < 300}))


# --- outcome states ---

def test_confirmed_after_one_successful_round():
    client = _FakeAnthropicClient([
        _tool_response("p1", _canned_probe()),
        _tool_response("r1", _canned_review(verdict="confident_enough")),
    ])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=5, transport=_transport_always(200))

    assert isinstance(result, BootstrapResult)
    assert result.status == "confirmed"
    assert result.schema.confirmed is True
    assert result.happy_day_example is not None
    assert len(result.probe_log) == 1


def test_inconclusive_when_budget_exhausted_with_a_success_but_never_confident():
    client = _FakeAnthropicClient([
        _tool_response("p1", _canned_probe()),
        _tool_response("r1", _canned_review(verdict="needs_more_probing")),
        _tool_response("p2", _canned_probe()),
        _tool_response("r2", _canned_review(verdict="needs_more_probing")),
    ])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=2, transport=_transport_always(200))

    assert result.status == "inconclusive"
    assert result.happy_day_example is not None
    assert result.schema.confirmed is False
    assert len(result.probe_log) == 2


def test_failed_when_budget_exhausted_with_zero_successes():
    client = _FakeAnthropicClient([
        _tool_response("p1", _canned_probe()),
        _tool_response("r1", _canned_review(verdict="needs_more_probing")),
        _tool_response("p2", _canned_probe()),
        _tool_response("r2", _canned_review(verdict="needs_more_probing")),
    ])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=2, transport=_transport_always(400))

    assert result.status == "failed"
    assert result.happy_day_example is None


def test_safety_net_overrides_a_bogus_confident_enough_with_zero_successes():
    # Round 1: probe fails (400), but the reviewer falsely claims
    # confident_enough anyway. The loop must not trust that - it should
    # force the verdict back to needs_more_probing and keep going, only
    # actually stopping once round 2 gets a real success and a genuine
    # confident_enough.
    responses_by_call = [
        _tool_response("p1", _canned_probe(path="/submit", body={"client_id": "a"})),
        _tool_response("r1", _canned_review(verdict="confident_enough")),  # bogus - zero successes so far
        _tool_response("p2", _canned_probe(path="/submit", body={"client_id": "b"})),
        _tool_response("r2", _canned_review(verdict="confident_enough")),  # genuine this time
    ]
    client = _FakeAnthropicClient(responses_by_call)

    call_count = {"n": 0}
    def handler(request):
        call_count["n"] += 1
        # First SUT call fails, second succeeds.
        return httpx.Response(400 if call_count["n"] == 1 else 200, json={})

    result = run_bootstrap_probe_loop(
        client, "http://test", _initial_schema(), max_probes=5, transport=httpx.MockTransport(handler),
    )

    assert result.status == "confirmed"
    assert len(result.probe_log) == 2  # both rounds actually ran - it didn't stop after round 1's bogus claim
    assert client.messages.call_count == 4  # p1, r1, p2, r2 - proves round 2 genuinely happened


def test_give_up_stops_the_loop_immediately_with_zero_probes_executed():
    client = _FakeAnthropicClient([_tool_response("p1", _canned_probe(give_up=True))])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=5, transport=_transport_always(200))

    assert result.status == "failed"
    assert result.probe_log == []
    assert client.messages.call_count == 1  # only the probe call - give_up short-circuits before any review


def test_happy_day_example_is_the_first_success_not_the_last():
    client = _FakeAnthropicClient([
        _tool_response("p1", _canned_probe(body={"client_id": "first-success"})),
        _tool_response("r1", _canned_review(verdict="needs_more_probing")),
        _tool_response("p2", _canned_probe(body={"client_id": "second-success"})),
        _tool_response("r2", _canned_review(verdict="confident_enough")),
    ])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=5, transport=_transport_always(200))

    assert result.status == "confirmed"
    assert result.happy_day_example["request"]["body"] == {"client_id": "first-success"}


def test_probe_log_entries_have_request_response_and_reasoning():
    client = _FakeAnthropicClient([
        _tool_response("p1", _canned_probe(reasoning="checking whether client_id alone is enough")),
        _tool_response("r1", _canned_review(verdict="confident_enough")),
    ])
    result = run_bootstrap_probe_loop(client, "http://test", _initial_schema(), max_probes=5, transport=_transport_always(200))

    [entry] = result.probe_log
    assert set(entry.keys()) == {"request", "response", "reasoning"}
    assert entry["reasoning"] == "checking whether client_id alone is enough"
    assert entry["response"]["status"] == 200


# --- validators ---

def test_validate_probe_response_accepts_well_formed_data():
    assert validate_probe_response(_canned_probe()) == []


def test_validate_probe_response_rejects_bad_method():
    errors = validate_probe_response(_canned_probe(method="FETCH"))
    assert any("method" in e for e in errors)


def test_validate_probe_response_rejects_non_dict_body():
    data = _canned_probe()
    data["body"] = "not a dict"
    errors = validate_probe_response(data)
    assert any("body" in e for e in errors)


def test_validate_review_response_accepts_well_formed_data():
    assert validate_review_response(_canned_review()) == []


def test_validate_review_response_rejects_bad_verdict():
    errors = validate_review_response(_canned_review(verdict="sure_why_not"))
    assert any("verdict" in e for e in errors)


def test_validate_review_response_rejects_too_few_gaps():
    data = _canned_review()
    data["gaps"] = ["only one"]
    errors = validate_review_response(data)
    assert any("gaps" in e for e in errors)


def test_validate_review_response_rejects_too_few_recommended_probes():
    data = _canned_review()
    data["recommended_next_probes"] = ["only one"]
    errors = validate_review_response(data)
    assert any("recommended_next_probes" in e for e in errors)


def test_validate_review_response_rejects_bad_field_type_in_updated_fields():
    data = _canned_review(updated_fields=[
        {"name": "x", "type": "not-a-real-type", "required": True, "enum": [], "description": ""},
    ])
    errors = validate_review_response(data)
    assert any("updated_fields[0].type" in e for e in errors)


# --- shared field_from_dict, not two independent copies ---

def test_freetext_and_probe_share_the_same_field_from_dict():
    assert freetext_field_from_dict is field_from_dict
    assert probe_field_from_dict is field_from_dict
