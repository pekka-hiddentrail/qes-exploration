"""Regression tests for the 3 issues an automated PR review flagged on
initial-core: non-string casting fields crashing unwrap_accidental_json_body,
non-positive RunConfig values crashing on an empty checkpoints list, and
non-JSON SUT responses crashing call_sut_once."""

import httpx
import pytest

from engine.adapters.token_purchase.adapter import validate_casting_response
from engine.config import RunConfig
from engine.http import call_sut_once


# --- validate_casting_response: reject wrong-typed fields before they reach
# unwrap_accidental_json_body(), which calls .strip() on them ---

def _valid_test(**overrides):
    base = {
        "linked_hypothesis": "", "auth_token": "tok_live_9f2c8a41", "card_number": "4111104332181963",
        "expiry_month": 11, "expiry_year": 2027, "cvv": "482", "credit_count": 10,
        "predicted_outcome": "approved", "predicted_status": "approved", "predicted_decline_reason": "",
    }
    base.update(overrides)
    return base


def test_validate_casting_response_accepts_well_typed_test():
    data = {"give_up": False, "reasoning": "x", "candidate_tests": [_valid_test()]}
    assert validate_casting_response(data) == []


@pytest.mark.parametrize("field,bad_value", [
    ("auth_token", 12345),
    ("card_number", None),
    ("cvv", ["482"]),
    ("linked_hypothesis", 0),
])
def test_validate_casting_response_rejects_non_string_fields(field, bad_value):
    data = {"give_up": False, "reasoning": "x", "candidate_tests": [_valid_test(**{field: bad_value})]}
    errors = validate_casting_response(data)
    assert any(field in e and "string" in e for e in errors)


@pytest.mark.parametrize("field,bad_value", [
    ("expiry_month", "11"),
    ("expiry_year", None),
    ("credit_count", "10"),
])
def test_validate_casting_response_rejects_non_int_fields(field, bad_value):
    data = {"give_up": False, "reasoning": "x", "candidate_tests": [_valid_test(**{field: bad_value})]}
    errors = validate_casting_response(data)
    assert any(field in e and "integer" in e for e in errors)


# --- RunConfig: reject non-positive values that would otherwise silently
# empty out the checkpoint loop and crash on checkpoints[-1] ---

@pytest.mark.parametrize("field,bad_value", [
    ("max_checkpoints", 0),
    ("max_checkpoints", -1),
    ("first_round_test_budget", 0),
    ("default_test_budget", -3),
    ("max_attempts", 0),
])
def test_run_config_rejects_non_positive_values(field, bad_value):
    with pytest.raises(ValueError, match=field):
        RunConfig(**{field: bad_value})


def test_run_config_accepts_valid_values():
    rc = RunConfig(max_checkpoints=2, first_round_test_budget=5, default_test_budget=4, max_attempts=3)
    assert rc.max_checkpoints == 2


# --- call_sut_once: a non-JSON response shouldn't crash the run ---

def test_call_sut_once_handles_non_json_response():
    def handler(request):
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    result = call_sut_once("http://test", "/purchase", {"foo": "bar"}, transport=httpx.MockTransport(handler))
    assert result["status"] == 502
    assert result["body"]["error"] == "non-JSON response from SUT"
    assert "Bad Gateway" in result["body"]["raw_text"]


def test_call_sut_once_parses_valid_json_response():
    def handler(request):
        return httpx.Response(200, json={"status": "approved"})

    result = call_sut_once("http://test", "/purchase", {"foo": "bar"}, transport=httpx.MockTransport(handler))
    assert result["status"] == 200
    assert result["body"] == {"status": "approved"}
