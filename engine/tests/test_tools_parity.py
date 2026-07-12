"""Asserts engine.tools and the token_purchase adapter's per-SUT pieces are
unchanged from experiments/token-purchase-poc/run_live.py - the literal
contract this port must not silently drift from. Loads the original module
directly from its file path (it's not an importable package)."""

import importlib.util
import sys
from pathlib import Path

import pytest

from engine import tools as engine_tools
from engine.adapters.token_purchase import adapter as token_purchase_adapter

REPO_ROOT = Path(__file__).resolve().parents[2]
ORIGINAL_DIR = REPO_ROOT / "experiments" / "token-purchase-poc"


@pytest.fixture(scope="module")
def original():
    sys.path.insert(0, str(ORIGINAL_DIR))
    try:
        spec = importlib.util.spec_from_file_location("original_token_purchase_run_live", ORIGINAL_DIR / "run_live.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path.remove(str(ORIGINAL_DIR))


# --- Domain-agnostic engine.tools vs. the original's most-evolved version ---

def test_hypothesis_tool_schema_matches(original):
    assert engine_tools.HYPOTHESIS_TOOL == original.HYPOTHESIS_TOOL


def test_skeptic_tool_schema_is_a_superset_of_the_original(original):
    # Deliberate divergence: engine.tools added a required coverage_breadth_check
    # field after a live run showed "strong_enough" firing on 5 narrow tests that
    # never touched most of the documented interface - the Skeptic conceded real
    # gaps in its own critique but the verdict schema had no way to weigh breadth
    # of coverage, only depth of evidence for individual claims. Everything the
    # original required must still be required; the new field is additive.
    original_props = set(original.SKEPTIC_TOOL["input_schema"]["properties"])
    engine_props = set(engine_tools.SKEPTIC_TOOL["input_schema"]["properties"])
    assert original_props <= engine_props
    assert "coverage_breadth_check" in engine_props - original_props

    original_required = set(original.SKEPTIC_TOOL["input_schema"]["required"])
    engine_required = set(engine_tools.SKEPTIC_TOOL["input_schema"]["required"])
    assert original_required <= engine_required
    assert "coverage_breadth_check" in engine_required

    assert engine_tools.SKEPTIC_TOOL["input_schema"]["properties"]["verdict"]["enum"] == ["weak", "strong_enough"]


def test_bug_report_tool_schema_matches(original):
    assert engine_tools.BUG_REPORT_TOOL == original.BUG_REPORT_TOOL


def test_hypothesis_system_prompt_matches(original):
    assert engine_tools.HYPOTHESIS_SYSTEM_PROMPT == original.HYPOTHESIS_SYSTEM_PROMPT


def test_skeptic_system_prompt_still_covers_the_original_material_reasons(original):
    # Deliberately no longer byte-identical (see test_skeptic_tool_schema_is_a_superset_of_the_original)
    # - check the original's core teaching content is still present, plus the new one.
    for phrase in ("inference_validity_check", "your_own_prior_review"):
        assert phrase in original.SKEPTIC_SYSTEM_PROMPT
        assert phrase in engine_tools.SKEPTIC_SYSTEM_PROMPT
    assert "coverage_breadth_check" in engine_tools.SKEPTIC_SYSTEM_PROMPT
    assert "coverage_breadth_check" not in original.SKEPTIC_SYSTEM_PROMPT


def test_bug_report_system_prompt_matches(original):
    # One deliberate difference: the original leaked domain vocabulary ("real
    # auth_token/card_number/expiry/cvv/credit_count values") into an otherwise
    # domain-agnostic prompt. engine.tools generalizes this to "real values" -
    # content must match after that substitution; whitespace/line-wrap can
    # differ since the shorter phrase reflows the paragraph.
    original_generalized = original.BUG_REPORT_SYSTEM_PROMPT.replace(
        "real auth_token/card_number/expiry/cvv/credit_count values", "real values"
    )
    normalize = lambda text: " ".join(text.split())
    assert normalize(engine_tools.BUG_REPORT_SYSTEM_PROMPT) == normalize(original_generalized)


def test_hypothesis_and_bug_report_validator_behavior_matches_on_sample_inputs(original):
    # Unaffected by the coverage_breadth_check addition - these should still match exactly.
    sample_bad_hyp = {"observed_behavior": "x"}
    assert engine_tools.validate_hypothesis_response(sample_bad_hyp) == original.validate_hypothesis_response(sample_bad_hyp)

    sample_bad_bugs = {"bugs": []}
    assert engine_tools.validate_bug_reports(sample_bad_bugs) == original.validate_bug_reports(sample_bad_bugs)


def test_skeptic_validator_requires_coverage_breadth_check():
    sample_missing_new_field = {
        "verdict": "weak",
        "gaps": ["a", "b"],
        "inference_validity_check": "n/a",
        "anomaly_critique": "x",
        "recommended_next_tests": ["a", "b"],
        "prior_critique_addressed": "n/a",
        "reasoning": "x",
    }
    errors = engine_tools.validate_skeptic_response(sample_missing_new_field)
    assert "missing required field 'coverage_breadth_check'" in errors

    sample_complete = {**sample_missing_new_field, "coverage_breadth_check": "x"}
    assert engine_tools.validate_skeptic_response(sample_complete) == []


# --- token_purchase adapter's per-SUT pieces vs. the original ---

def test_casting_tool_schema_matches(original):
    assert token_purchase_adapter.CASTING_TOOL == original.CASTING_TOOL


def test_casting_system_prompt_matches(original):
    for budget, is_first in ((12, True), (8, False)):
        assert token_purchase_adapter.casting_system_prompt(budget, is_first) == original.casting_system_prompt(budget, is_first)


def test_known_accounts_and_schema_doc_match(original):
    assert token_purchase_adapter.KNOWN_ACCOUNTS == original.KNOWN_ACCOUNTS
    assert token_purchase_adapter.API_SCHEMA_DOC == original.API_SCHEMA_DOC
    assert token_purchase_adapter.KNOWN_DECLINE_REASONS == original.KNOWN_DECLINE_REASONS


def test_validate_casting_response_matches_on_sample_inputs(original):
    sample = {"give_up": False, "reasoning": "x", "candidate_tests": []}
    assert token_purchase_adapter.validate_casting_response(sample) == original.validate_casting_response(sample)
