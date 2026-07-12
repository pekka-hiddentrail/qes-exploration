"""Default history-redaction for evidence shown back to the Driver/Skeptic.
An adapter may override this via SUTAdapter.redact_history_for_model if a
domain needs to hide something more than round bookkeeping."""

import json


def default_redact_history_for_model(casting_log: list[dict]) -> list[dict]:
    """No literal content needs hiding by default - this just strips
    round_reasoning/checkpoint bookkeeping so evidence stays focused on
    outcomes, not re-feeding the model its own prior reasoning verbatim."""
    redacted = [{k: v for k, v in entry.items() if k not in ("round_reasoning",)} for entry in casting_log]
    return json.loads(json.dumps(redacted))
