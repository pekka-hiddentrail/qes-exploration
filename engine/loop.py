"""The generic Driver+Skeptic checkpoint loop: propose and execute a batch of
tests, form one hypothesis about behavior and any anomalies noticed, get a
cold Skeptic review, and either continue (Skeptic says "weak") or conclude
(Skeptic says "strong_enough" or the checkpoint cap is reached). Ported
almost verbatim from token-purchase-poc/run_live.py's unified checkpoint
loop - every module-global constant becomes a run_config/adapter read, and
every domain-specific call (test proposal schema, execute_test, prediction
matching) becomes an adapter read.
"""

import json

from anthropic import Anthropic

from engine.adapter import SUTAdapter
from engine.client import call_tool_with_retry
from engine.config import RunConfig
from engine.http import call_sut_once
from engine.redact import default_redact_history_for_model
from engine.tools import (
    BUG_REPORT_SYSTEM_PROMPT,
    BUG_REPORT_TOOL,
    HYPOTHESIS_SYSTEM_PROMPT,
    HYPOTHESIS_TOOL,
    SKEPTIC_SYSTEM_PROMPT,
    SKEPTIC_TOOL,
    validate_bug_reports,
    validate_hypothesis_response,
    validate_skeptic_response,
)


def _redact(adapter: SUTAdapter, casting_log: list[dict]) -> list[dict]:
    redact_fn = adapter.redact_history_for_model or default_redact_history_for_model
    return redact_fn(casting_log)


def _base_evidence(adapter: SUTAdapter, happy_day_example: dict) -> dict:
    return {
        "api_schema": adapter.api_schema_doc,
        **adapter.onboarding_extra,
        "happy_day_example": happy_day_example,
    }


def get_happy_day_example(adapter: SUTAdapter) -> dict:
    request = {"method": "POST", "path": adapter.test_endpoint_path, "body": adapter.happy_day_request}
    response = call_sut_once(adapter.base_url, adapter.test_endpoint_path, adapter.happy_day_request)
    return {"request": request, "response": response}


def get_casting_round(
    client: Anthropic,
    adapter: SUTAdapter,
    run_config: RunConfig,
    happy_day_example: dict,
    casting_log: list[dict],
    prior_checkpoint_feedback: dict | None = None,
    *,
    test_budget: int,
    is_first_round: bool,
) -> dict:
    evidence = {
        **_base_evidence(adapter, happy_day_example),
        "tests_tried_in_earlier_rounds": _redact(adapter, casting_log),
    }
    if prior_checkpoint_feedback is not None:
        evidence["prior_checkpoint_feedback"] = prior_checkpoint_feedback
    return call_tool_with_retry(
        client,
        model=run_config.model,
        system=adapter.casting_system_prompt(test_budget, is_first_round),
        tools=[adapter.casting_tool_schema],
        tool_name="submit_casting_round",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=adapter.validate_casting_response,
        max_tokens=adapter.casting_max_tokens(test_budget),
        max_attempts=run_config.max_attempts,
    )


def get_checkpoint_hypothesis(
    client: Anthropic,
    adapter: SUTAdapter,
    run_config: RunConfig,
    happy_day_example: dict,
    casting_log: list[dict],
    prior_skeptic_review: dict | None = None,
) -> dict:
    evidence = {
        **_base_evidence(adapter, happy_day_example),
        "all_tests_this_session": _redact(adapter, casting_log),
    }
    if prior_skeptic_review is not None:
        evidence["prior_skeptic_review"] = prior_skeptic_review
    return call_tool_with_retry(
        client,
        model=run_config.model,
        system=HYPOTHESIS_SYSTEM_PROMPT,
        tools=[HYPOTHESIS_TOOL],
        tool_name="submit_checkpoint_hypothesis",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_hypothesis_response,
        max_tokens=2560,
        max_attempts=run_config.max_attempts,
    )


def get_skeptic_review(
    client: Anthropic, run_config: RunConfig, hypothesis: dict, prior_skeptic_review: dict | None = None
) -> dict:
    evidence = {
        "observed_behavior": hypothesis["observed_behavior"],
        "anomalies": hypothesis["anomalies"],
        "untested_areas": hypothesis["untested_areas"],
        "prior_gaps_response": hypothesis.get("prior_gaps_response", []),
    }
    if prior_skeptic_review is not None:
        evidence["your_own_prior_review"] = prior_skeptic_review
    return call_tool_with_retry(
        client,
        model=run_config.model,
        system=SKEPTIC_SYSTEM_PROMPT,
        tools=[SKEPTIC_TOOL],
        tool_name="submit_skeptic_review",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_skeptic_response,
        max_tokens=3072,
        max_attempts=run_config.max_attempts,
    )


def run_checkpoint_loop(
    client: Anthropic,
    adapter: SUTAdapter,
    run_config: RunConfig,
    happy_day_example: dict,
    test_counter,
    on_checkpoint=None,
):
    """Returns (casting_log, checkpoints, stopped_reason).

    on_checkpoint(casting_log, checkpoints), if given, is called after every
    checkpoint completes - not just once at the end - so a crash partway
    through (a non-retryable API error, an unexpected bug) doesn't discard
    checkpoints that already finished. Each call is a full, self-consistent
    snapshot; the caller decides what to do with it (e.g. write it to disk).
    """
    casting_log = []
    checkpoints = []
    prior_feedback = None
    stopped_reason = "checkpoints_exhausted"

    for checkpoint_num in range(1, run_config.max_checkpoints + 1):
        is_first_checkpoint = checkpoint_num == 1
        test_budget = run_config.first_round_test_budget if is_first_checkpoint else run_config.default_test_budget
        print(f"Asking Claude for a casting round (checkpoint {checkpoint_num}, budget {test_budget})...")
        casting = get_casting_round(
            client,
            adapter,
            run_config,
            happy_day_example,
            casting_log,
            prior_feedback,
            test_budget=test_budget,
            is_first_round=is_first_checkpoint,
        )

        if casting["give_up"]:
            print(f"  Claude gave up casting: {casting['reasoning']}")
        else:
            print(f"  round reasoning: {casting['reasoning']}")
            for test in casting["candidate_tests"]:
                linked = test["linked_hypothesis"]
                label = f"hypothesis: {linked}" if linked else "edge case"
                test_number = next(test_counter)
                detail = adapter.describe_test_for_log(test) if adapter.describe_test_for_log else str(
                    {k: v for k, v in test.items() if k != "linked_hypothesis"}
                )
                print(f"  test #{test_number} ({label}): {detail}")

                result = adapter.execute_test(test, test_number)
                casting_log.append({
                    "checkpoint": checkpoint_num,
                    "round": 1,
                    "round_reasoning": casting["reasoning"],
                    "linked_hypothesis": linked,
                    **result,
                })
                result_detail = adapter.describe_result_for_log(result) if adapter.describe_result_for_log else str(result.get("response", {}).get("body", {}))
                print(f"    actual: {result_detail} - prediction {'matched' if result['prediction_matched'] else 'MISSED'}")

        prior_skeptic_review = prior_feedback["skeptic_review"] if prior_feedback else None

        print(f"Checkpoint {checkpoint_num}: forming a hypothesis...")
        hypothesis = get_checkpoint_hypothesis(client, adapter, run_config, happy_day_example, casting_log, prior_skeptic_review)
        print(f"  observed_behavior: {hypothesis['observed_behavior']}")
        print(f"  anomalies noticed: {len(hypothesis['anomalies'])}")
        if hypothesis["prior_gaps_response"]:
            print(f"  prior gaps responded to: {len(hypothesis['prior_gaps_response'])}")

        print("Asking Skeptic for a cold review...")
        skeptic_review = get_skeptic_review(client, run_config, hypothesis, prior_skeptic_review)
        print(f"  skeptic verdict: {skeptic_review['verdict']}")

        checkpoints.append({"checkpoint": checkpoint_num, "hypothesis": hypothesis, "skeptic_review": skeptic_review})

        if on_checkpoint is not None:
            on_checkpoint(casting_log, checkpoints)

        if skeptic_review["verdict"] == "strong_enough":
            stopped_reason = "skeptic_satisfied"
            break

        prior_feedback = {"hypothesis": hypothesis, "skeptic_review": skeptic_review}

    return casting_log, checkpoints, stopped_reason


def get_bug_reports(
    client: Anthropic,
    adapter: SUTAdapter,
    run_config: RunConfig,
    final_hypothesis: dict,
    final_skeptic_review: dict,
    stopped_reason: str,
    casting_log: list[dict],
) -> list[dict]:
    evidence = {
        "final_hypothesis": final_hypothesis,
        "final_skeptic_review": final_skeptic_review,
        "stopped_reason": stopped_reason,
        "all_tests_this_session": _redact(adapter, casting_log),
    }
    result = call_tool_with_retry(
        client,
        model=run_config.model,
        system=BUG_REPORT_SYSTEM_PROMPT,
        tools=[BUG_REPORT_TOOL],
        tool_name="submit_bug_reports",
        user_message=json.dumps(evidence, indent=2),
        validate_fn=validate_bug_reports,
        max_tokens=3072,
        max_attempts=run_config.max_attempts,
    )
    return result["bugs"]
