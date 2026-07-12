"""run_checkpoint_loop's on_checkpoint callback: called after every
checkpoint completes, not just once at the end, so a crash partway through
doesn't discard checkpoints that already finished. Verified by stubbing out
the Anthropic-calling functions directly (get_casting_round/
get_checkpoint_hypothesis/get_skeptic_review) - no network calls, no real
LLM behavior involved, purely checking the loop's own bookkeeping."""

import itertools

import engine.loop as loop
from engine.adapter import SUTAdapter
from engine.config import RunConfig

_FAKE_ADAPTER = SUTAdapter(
    name="fake",
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


def _make_fakes(num_checkpoints_before_strong_enough):
    """Checkpoint verdicts are 'weak' until the Nth, then 'strong_enough'."""
    calls = {"casting": 0, "hypothesis": 0, "skeptic": 0}

    def fake_casting_round(client, adapter, run_config, happy_day_example, casting_log, prior_feedback, *, test_budget, is_first_round):
        calls["casting"] += 1
        return {
            "give_up": False,
            "reasoning": "r",
            "candidate_tests": [{
                "linked_hypothesis": "", "predicted_outcome": "x",
            }],
        }

    def fake_hypothesis(client, adapter, run_config, happy_day_example, casting_log, prior_skeptic_review=None):
        calls["hypothesis"] += 1
        return {"observed_behavior": "b", "anomalies": [], "untested_areas": ["u"], "prior_gaps_response": []}

    def fake_skeptic(client, run_config, hypothesis, prior_skeptic_review=None):
        calls["skeptic"] += 1
        verdict = "strong_enough" if calls["skeptic"] >= num_checkpoints_before_strong_enough else "weak"
        return {
            "verdict": verdict, "gaps": ["g1", "g2"], "coverage_breadth_check": "c",
            "inference_validity_check": "n/a", "anomaly_critique": "n/a",
            "recommended_next_tests": ["t1", "t2"], "prior_critique_addressed": "n/a", "reasoning": "r",
        }

    return calls, fake_casting_round, fake_hypothesis, fake_skeptic


def test_on_checkpoint_called_once_per_checkpoint_with_growing_state(monkeypatch):
    calls, fake_casting_round, fake_hypothesis, fake_skeptic = _make_fakes(num_checkpoints_before_strong_enough=3)
    monkeypatch.setattr(loop, "get_casting_round", fake_casting_round)
    monkeypatch.setattr(loop, "get_checkpoint_hypothesis", fake_hypothesis)
    monkeypatch.setattr(loop, "get_skeptic_review", fake_skeptic)

    snapshots = []

    def on_checkpoint(casting_log, checkpoints):
        # Record independent copies - the real lists keep mutating after this
        # call returns, so a snapshot must not just hold a reference to them.
        snapshots.append((len(casting_log), len(checkpoints)))

    run_config = RunConfig(max_checkpoints=5)
    casting_log, checkpoints, stopped_reason = loop.run_checkpoint_loop(
        client=None, adapter=_FAKE_ADAPTER, run_config=run_config, happy_day_example={},
        test_counter=itertools.count(1), on_checkpoint=on_checkpoint,
    )

    assert stopped_reason == "skeptic_satisfied"
    assert len(checkpoints) == 3
    # on_checkpoint fires exactly once per checkpoint, with the count growing each time.
    assert snapshots == [(1, 1), (2, 2), (3, 3)]


def test_on_checkpoint_reflects_partial_progress_if_loop_would_stop_at_cap(monkeypatch):
    # Verdict never reaches strong_enough - loop runs to the checkpoint cap.
    calls, fake_casting_round, fake_hypothesis, fake_skeptic = _make_fakes(num_checkpoints_before_strong_enough=999)
    monkeypatch.setattr(loop, "get_casting_round", fake_casting_round)
    monkeypatch.setattr(loop, "get_checkpoint_hypothesis", fake_hypothesis)
    monkeypatch.setattr(loop, "get_skeptic_review", fake_skeptic)

    snapshots = []
    run_config = RunConfig(max_checkpoints=2)
    casting_log, checkpoints, stopped_reason = loop.run_checkpoint_loop(
        client=None, adapter=_FAKE_ADAPTER, run_config=run_config, happy_day_example={},
        test_counter=itertools.count(1), on_checkpoint=lambda cl, cp: snapshots.append(len(cp)),
    )

    assert stopped_reason == "checkpoints_exhausted"
    assert snapshots == [1, 2]


def test_on_checkpoint_is_optional(monkeypatch):
    calls, fake_casting_round, fake_hypothesis, fake_skeptic = _make_fakes(num_checkpoints_before_strong_enough=1)
    monkeypatch.setattr(loop, "get_casting_round", fake_casting_round)
    monkeypatch.setattr(loop, "get_checkpoint_hypothesis", fake_hypothesis)
    monkeypatch.setattr(loop, "get_skeptic_review", fake_skeptic)

    # No on_checkpoint passed at all - must not raise.
    casting_log, checkpoints, stopped_reason = loop.run_checkpoint_loop(
        client=None, adapter=_FAKE_ADAPTER, run_config=RunConfig(max_checkpoints=3), happy_day_example={},
        test_counter=itertools.count(1),
    )
    assert stopped_reason == "skeptic_satisfied"
