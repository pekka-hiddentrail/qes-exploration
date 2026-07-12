"""Asserts the complex_sut adapter's per-SUT pieces are unchanged from
experiments/complex-sut-poc/run_live.py, except for two deliberate
divergences: the proactive addition of client_id/payload string-type checks
(the same class of gap fixed in the token_purchase adapter after PR review,
applied here before it could be hit a second time), and a fix to
_compute_correctness silently asserting "correct" when the SUT's responses
are missing the limit field entirely - a PR review on this very adapter
caught it."""

import importlib.util
import sys
from pathlib import Path

import pytest

from engine.adapters.complex_sut import adapter as complex_sut_adapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_DIR = REPO_ROOT / "experiments" / "complex-sut-poc"


@pytest.fixture(scope="module")
def original():
    sys.path.insert(0, str(ORIGINAL_DIR))
    try:
        spec = importlib.util.spec_from_file_location("original_complex_sut_run_live", ORIGINAL_DIR / "run_live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(ORIGINAL_DIR))


def test_casting_tool_schema_matches(original):
    assert complex_sut_adapter.CASTING_TOOL == original.CASTING_TOOL


def test_casting_system_prompt_matches(original):
    for budget, is_first in ((10, True), (6, False)):
        assert complex_sut_adapter.casting_system_prompt(budget, is_first) == original.casting_system_prompt(budget, is_first)


def test_api_schema_and_happy_day_request_match(original):
    assert complex_sut_adapter.API_SCHEMA_DOC == original.API_SCHEMA_DOC
    assert complex_sut_adapter.HAPPY_DAY_REQUEST == original.HAPPY_DAY_REQUEST


def test_max_request_count_matches(original):
    assert complex_sut_adapter.MAX_REQUEST_COUNT == original.MAX_REQUEST_COUNT


def test_validate_casting_response_matches_on_well_typed_sample(original):
    sample = {"give_up": False, "reasoning": "x", "candidate_tests": []}
    assert complex_sut_adapter.validate_casting_response(sample) == original.validate_casting_response(sample)


def test_validate_casting_response_now_also_rejects_non_string_client_id(original):
    # Deliberate divergence: the original never checked client_id/payload were
    # actually strings before passing them to unwrap_accidental_json_body(),
    # the same gap a PR review caught in the token_purchase adapter. Fixed
    # here proactively rather than waiting to hit it again.
    test = {
        "linked_hypothesis": "", "client_id": 12345, "payload": "x", "priority": "normal",
        "request_count": 1, "concurrent": False, "predicted_outcome": "x", "predicted_correctness": "correct",
    }
    data = {"give_up": False, "reasoning": "x", "candidate_tests": [test]}

    original_errors = original.validate_casting_response(data)
    assert not any("client_id" in e and "string" in e for e in original_errors)

    engine_errors = complex_sut_adapter.validate_casting_response(data)
    assert any("client_id" in e and "string" in e for e in engine_errors)


def test_compute_correctness_overcounted_when_accepted_exceeds_limit():
    responses = [{"body": {"status": "accepted", "used": i, "limit": 5}} for i in range(1, 8)]
    accepted_count, limit, actual_correctness = complex_sut_adapter._compute_correctness(responses)
    assert (accepted_count, limit, actual_correctness) == (7, 5, "overcounted")


def test_compute_correctness_correct_when_within_limit():
    responses = [{"body": {"status": "accepted", "used": i, "limit": 5}} for i in range(1, 4)]
    accepted_count, limit, actual_correctness = complex_sut_adapter._compute_correctness(responses)
    assert (accepted_count, limit, actual_correctness) == (3, 5, "correct")


def test_compute_correctness_unknown_when_limit_missing_from_every_response():
    # The bug a PR review caught: this used to silently default to "correct"
    # when every response was malformed/missing the limit field, asserting
    # something with no actual basis.
    responses = [{"body": {"error": "non-JSON response from SUT", "raw_text": "<html>502</html>"}}]
    accepted_count, limit, actual_correctness = complex_sut_adapter._compute_correctness(responses)
    assert (accepted_count, limit, actual_correctness) == (0, None, "unknown")
