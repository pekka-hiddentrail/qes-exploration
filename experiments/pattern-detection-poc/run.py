"""
Tests the "noticing" step this project's first PoC (hypothesis-poc) deliberately
skipped: given a raw sequence of calls with nothing pre-flagged as anomalous, can
Claude spot a trend, identify which call breaks it, and hypothesize why - producing a
confirm/disconfirm test pair like hypothesis-poc did.

Single scenario (sequence.json): 8 calls where response latency tracks input length,
then a 9th call of unremarkable length whose latency rockets. A good answer notices
the length trend is NOT what explains call 9, and proposes a length-independent cause
instead of just extending the same explanation.
"""

import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

MODEL = "claude-sonnet-4-6"
MAX_ATTEMPTS = 3

PATTERN_TOOL = {
    "name": "submit_pattern_hypothesis",
    "description": (
        "Submit the pattern you noticed across the call sequence, which call breaks "
        "it, a hypothesis for why, a competing explanation, and a confirm/disconfirm "
        "test pair designed to discriminate between them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "observed_pattern": {
                "type": "string",
                "description": "The general trend noticed across the sequence of calls.",
            },
            "anomalous_call_index": {
                "type": "integer",
                "description": "The index of the call that breaks the observed pattern.",
            },
            "claim": {
                "type": "string",
                "description": "The primary, narrow, falsifiable hypothesis for why that call breaks the pattern.",
            },
            "competing_explanation": {
                "type": "string",
                "description": "The most plausible alternative explanation a careful engineer would also consider.",
            },
            "severity_if_true": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "confirm_test": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "The concrete action/request to perform."},
                    "predicted_outcome_if_true": {"type": "string"},
                },
                "required": ["action", "predicted_outcome_if_true"],
            },
            "disconfirm_test": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "A concrete action whose outcome would genuinely surprise you if the claim were true.",
                    },
                    "predicted_outcome_if_false": {"type": "string"},
                },
                "required": ["action", "predicted_outcome_if_false"],
            },
            "why_this_discriminates": {
                "type": "string",
                "description": "Explain why the disconfirm test's outcome would differ between the claim and the competing explanation, not just repeat the same check.",
            },
        },
        "required": [
            "observed_pattern",
            "anomalous_call_index",
            "claim",
            "competing_explanation",
            "severity_if_true",
            "confirm_test",
            "disconfirm_test",
            "why_this_discriminates",
        ],
    },
}

SYSTEM_PROMPT = """You are investigating a sequence of API calls made against a system under test (SUT).
Nothing in the data is pre-flagged as anomalous - you are given the raw sequence of requests and
responses and must notice any pattern yourself, and notice if any call breaks that pattern.
You do NOT have access to ground truth - form your best hypothesis using only the evidence given.

Produce:
1. The general pattern you notice across the sequence.
2. Which single call breaks that pattern.
3. The most likely, narrow, falsifiable hypothesis for why that call breaks the pattern - not
   just an extension of the general pattern's explanation.
4. The most plausible competing explanation for the same evidence - a genuine alternative a
   careful engineer would also consider, not a strawman.
5. A confirm test and a disconfirm test. The disconfirm test must be designed so its outcome
   would differ depending on which explanation is true - not simply repeat the same check.

Call submit_pattern_hypothesis with your answer."""


def validate_pattern_hypothesis(data) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    required_top = [
        "observed_pattern",
        "anomalous_call_index",
        "claim",
        "competing_explanation",
        "severity_if_true",
        "confirm_test",
        "disconfirm_test",
        "why_this_discriminates",
    ]
    for key in required_top:
        if key not in data:
            errors.append(f"missing required field '{key}'")

    if "anomalous_call_index" in data and not isinstance(data["anomalous_call_index"], int):
        errors.append("'anomalous_call_index' must be an integer")

    for test_key, outcome_key in [
        ("confirm_test", "predicted_outcome_if_true"),
        ("disconfirm_test", "predicted_outcome_if_false"),
    ]:
        test = data.get(test_key)
        if test is None:
            continue
        if not isinstance(test, dict):
            errors.append(f"'{test_key}' must be an object, got {type(test).__name__}")
            continue
        for sub_key in ("action", outcome_key):
            if sub_key not in test:
                errors.append(f"'{test_key}' missing required field '{sub_key}'")

    if data.get("severity_if_true") not in ("high", "medium", "low"):
        errors.append("'severity_if_true' must be one of high/medium/low")

    return errors


def run_sequence(client: Anthropic, scenario: dict) -> dict:
    evidence = {"scenario": scenario["scenario"], "calls": scenario["calls"]}
    user_message = json.dumps(evidence, indent=2)

    last_errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = client.messages.create(
            model=MODEL,
            max_tokens=1536,
            system=SYSTEM_PROMPT,
            tools=[PATTERN_TOOL],
            tool_choice={"type": "tool", "name": "submit_pattern_hypothesis"},
            messages=[{"role": "user", "content": user_message}],
        )

        tool_use = next(block for block in message.content if block.type == "tool_use")
        errors = validate_pattern_hypothesis(tool_use.input)
        if not errors:
            return tool_use.input

        last_errors = errors
        print(f"  attempt {attempt} produced malformed output: {errors} - retrying")

    raise RuntimeError(f"Gave up after {MAX_ATTEMPTS} attempts, last errors: {last_errors}")


def main():
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY in .env (see .env.example)")

    client = Anthropic(api_key=api_key)

    base_dir = Path(__file__).parent
    scenario = json.loads((base_dir / "sequence.json").read_text())

    print("Running sequence...")
    entry = {"ground_truth": scenario["ground_truth"]}
    try:
        entry["model_output"] = run_sequence(client, scenario)
    except RuntimeError as e:
        entry["error"] = str(e)

    out_dir = base_dir / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "output.json"
    out_path.write_text(json.dumps(entry, indent=2))
    print(f"\nWrote result to {out_path}")
    print("Now score it by hand against rubric.md.")


if __name__ == "__main__":
    main()
