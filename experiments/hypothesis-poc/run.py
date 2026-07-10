"""
Minimal test of the hypothesis-generation / disconfirmation-design step described in
docs/exploratory-testing-engine-concept.md, section 3.6-3.7.

For each hand-built case in cases.json, sends the request/response evidence (but NOT
ground_truth) to Claude and asks it to produce a primary hypothesis, a competing
explanation, and a confirm/disconfirm test pair. Results are written to
results/output.json for manual scoring against rubric.md.
"""

import json
import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

MODEL = "claude-sonnet-4-6"
MAX_ATTEMPTS = 3

HYPOTHESIS_TOOL = {
    "name": "submit_hypothesis",
    "description": (
        "Submit the primary hypothesis, a competing alternative explanation, and a "
        "confirm/disconfirm test pair designed to discriminate between them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claim": {
                "type": "string",
                "description": "The primary, narrow, falsifiable hypothesis explaining the anomaly.",
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
            "claim",
            "competing_explanation",
            "severity_if_true",
            "confirm_test",
            "disconfirm_test",
            "why_this_discriminates",
        ],
    },
}

SYSTEM_PROMPT = """You are investigating an anomaly found while testing a system under test (SUT).
You are given the request/response evidence, what was expected (per spec, history, or internal
consistency), and what was actually observed. You do NOT have access to ground truth - form your
best hypothesis using only the evidence given.

Produce:
1. The most likely, narrow, falsifiable hypothesis for what is actually happening.
2. The most plausible competing explanation for the same evidence - a genuine alternative a
   careful engineer would also consider, not a strawman.
3. A confirm test and a disconfirm test. The disconfirm test must be designed so its outcome
   would differ depending on which explanation is true - not simply repeat the same check.

Call submit_hypothesis with your answer."""


def build_user_message(case: dict) -> str:
    evidence = {k: v for k, v in case.items() if k not in ("id", "category", "ground_truth")}
    return json.dumps(evidence, indent=2)


def validate_hypothesis(data) -> list[str]:
    """Check the tool_use.input actually matches the schema we asked for.

    Anthropic's tool_choice guarantees the model calls the named tool, not that its
    input matches input_schema - a model can still emit a string where an object was
    required, or drop a required field. This caught exactly that on case 04.
    """
    errors = []
    if not isinstance(data, dict):
        return [f"expected an object, got {type(data).__name__}"]

    required_top = [
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


def run_case(client: Anthropic, case: dict) -> dict:
    last_errors = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[HYPOTHESIS_TOOL],
            tool_choice={"type": "tool", "name": "submit_hypothesis"},
            messages=[{"role": "user", "content": build_user_message(case)}],
        )

        tool_use = next(block for block in message.content if block.type == "tool_use")
        errors = validate_hypothesis(tool_use.input)
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
    cases = json.loads((base_dir / "cases.json").read_text())

    results = []
    for case in cases:
        print(f"Running {case['id']}...")
        entry = {"id": case["id"], "category": case["category"], "ground_truth": case["ground_truth"]}
        try:
            entry["model_output"] = run_case(client, case)
        except RuntimeError as e:
            entry["error"] = str(e)
        results.append(entry)

    out_dir = base_dir / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "output.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {len(results)} results to {out_path}")
    print("Now score each case by hand against rubric.md.")


if __name__ == "__main__":
    main()
