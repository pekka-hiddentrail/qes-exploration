"""Anthropic client construction and the shared call->validate->retry loop
used by every tool-forced call in the checkpoint loop. Identical logic
existed in every prior experiment's run_live.py."""

import os

from anthropic import Anthropic
from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_ATTEMPTS = 3


def build_client() -> Anthropic:
    load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("Set ANTHROPIC_API_KEY in .env (see .env.example)")
    return Anthropic(api_key=api_key)


def call_tool_with_retry(
    client, *, model, system, tools, tool_name, user_message, validate_fn, max_tokens, max_attempts=DEFAULT_MAX_ATTEMPTS
):
    """Retries are informed, not blind repeats: on failure, the model's own malformed call and
    the concrete validation errors are fed back as a tool_result before asking again, so a
    systematic misunderstanding (e.g. an omitted required field) has a chance to self-correct
    instead of reproducing the identical mistake on every attempt.
    """
    messages = [{"role": "user", "content": user_message}]
    last_errors = ["no attempts made"]
    for attempt in range(1, max_attempts + 1):
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )

        tool_use = next((block for block in message.content if block.type == "tool_use"), None)
        if tool_use is None:
            last_errors = [f"no tool_use block in response (stop_reason={message.stop_reason})"]
            print(f"  attempt {attempt} produced no tool call: {last_errors} - retrying")
            messages.append({"role": "assistant", "content": message.content})
            messages.append({"role": "user", "content": "You must call the tool. Try again."})
            continue

        errors = validate_fn(tool_use.input)
        if not errors:
            return tool_use.input

        last_errors = errors
        print(f"  attempt {attempt} produced malformed output: {errors} - retrying")
        messages.append({"role": "assistant", "content": message.content})
        messages.append({
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": "Invalid: " + "; ".join(errors) + ". Fix and call the tool again with a corrected, complete answer.",
                "is_error": True,
            }],
        })

    raise RuntimeError(f"Gave up after {max_attempts} attempts, last errors: {last_errors}")
