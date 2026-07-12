"""Small, generic helpers with no domain knowledge - identical logic existed
in every prior experiment's run_live.py, just applied to different field
names."""

import json


def unwrap_accidental_json_body(text: str) -> str:
    """Defends against the Driver wrapping a field's value in a JSON envelope
    (e.g. '{"auth_token": "..."}') instead of returning the raw string."""
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and len(parsed) == 1:
                (value,) = parsed.values()
                if isinstance(value, str):
                    return value
        except json.JSONDecodeError:
            pass
    return text
