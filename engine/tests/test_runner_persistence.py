"""engine.runner.run() writes output.json incrementally after every
checkpoint, not just once at the end - verified by making a checkpoint
raise partway through and confirming the earlier, already-completed
checkpoints survive on disk rather than being lost with the crash. No
network calls: the SUT readiness probe, happy-day fetch, and the three
Anthropic-calling functions inside the loop are all stubbed."""

import itertools
import json

import httpx
import pytest

import engine.loop as loop
import engine.runner as runner
from engine.adapter import SUTAdapter
from engine.config import RunConfig

_FAKE_ADAPTER = SUTAdapter(
    name="fake_runner_test",
    display_name="Fake",
    base_url="http://example.invalid",
    test_endpoint_path="/x",
    casting_tool_schema={},
    casting_system_prompt=lambda budget, is_first: "",
    validate_casting_response=lambda data: [],
    execute_test=lambda test, test_number: {
        "test_number": test_number, "request": {}, "response": {}, "predicted_outcome": "", "prediction_matched": True,
    },
    render_test_entry=lambda entry: "",
    render_onboarding_section=lambda *a: "",
)


@pytest.fixture(autouse=True)
def _stub_sut_io(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: httpx.Response(200))
    monkeypatch.setattr(runner, "get_happy_day_example", lambda adapter: {"request": {"body": {}}, "response": {"body": {}}})
    monkeypatch.setattr(runner, "build_client", lambda: object())


def _fake_casting_round(client, adapter, run_config, happy_day_example, casting_log, prior_feedback, *, test_budget, is_first_round):
    return {"give_up": False, "reasoning": "r", "candidate_tests": [{"linked_hypothesis": "", "predicted_outcome": "x"}]}


def _fake_hypothesis(client, adapter, run_config, happy_day_example, casting_log, prior_skeptic_review=None):
    return {"observed_behavior": "b", "anomalies": [], "untested_areas": ["u"], "prior_gaps_response": []}


def _skeptic_review(verdict):
    return {
        "verdict": verdict, "gaps": ["g1", "g2"], "coverage_breadth_check": "c",
        "inference_validity_check": "n/a", "anomaly_critique": "n/a",
        "recommended_next_tests": ["t1", "t2"], "prior_critique_addressed": "n/a", "reasoning": "r",
    }


def test_output_json_written_incrementally_and_survives_a_mid_run_crash(monkeypatch, tmp_path):
    checkpoint_count = {"n": 0}

    def flaky_skeptic(client, run_config, hypothesis, prior_skeptic_review=None):
        checkpoint_count["n"] += 1
        if checkpoint_count["n"] == 2:
            raise ValueError("simulated unexpected failure mid-run")
        return _skeptic_review("weak")

    monkeypatch.setattr(loop, "get_casting_round", _fake_casting_round)
    monkeypatch.setattr(loop, "get_checkpoint_hypothesis", _fake_hypothesis)
    monkeypatch.setattr(loop, "get_skeptic_review", flaky_skeptic)

    run_config = RunConfig(max_checkpoints=4, out_dir=tmp_path)
    output = runner.run(_FAKE_ADAPTER, run_config)

    # The run as a whole reports the failure...
    assert "error" in output
    assert "simulated unexpected failure" in output["error"]

    # ...but checkpoint 1, which completed before the crash, was written to
    # disk by save_progress and is not lost.
    on_disk = json.loads((tmp_path / "output.json").read_text())
    assert len(on_disk["checkpoints"]) == 1
    assert on_disk["checkpoints"][0]["hypothesis"]["observed_behavior"] == "b"


def test_output_json_reflects_final_state_on_a_clean_run(monkeypatch, tmp_path):
    monkeypatch.setattr(loop, "get_casting_round", _fake_casting_round)
    monkeypatch.setattr(loop, "get_checkpoint_hypothesis", _fake_hypothesis)
    monkeypatch.setattr(loop, "get_skeptic_review", lambda *a, **kw: _skeptic_review("strong_enough"))

    run_config = RunConfig(max_checkpoints=4, out_dir=tmp_path)
    output = runner.run(_FAKE_ADAPTER, run_config)

    assert "error" not in output
    assert output["stopped_reason"] == "skeptic_satisfied"
    on_disk = json.loads((tmp_path / "output.json").read_text())
    assert on_disk["stopped_reason"] == "skeptic_satisfied"
    assert len(on_disk["checkpoints"]) == 1
