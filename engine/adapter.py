"""The SUTAdapter interface: everything genuinely per-SUT that the generic
checkpoint loop and report renderer need supplied. A frozen dataclass rather
than a Protocol/ABC - matches the existing procedural style (free functions +
module constants) that each experiment already used, with no self/inheritance
boilerplate to invent. One instance is built at import time and only ever
read during a run.

engine/ code must never import from engine/adapters/ - adapters import from
engine, never the reverse. This module has no knowledge of any concrete
adapter.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

_DEFAULT_CASTING_MAX_TOKENS: Callable[[int], int] = lambda budget: 4096 if budget <= 8 else 6144


@dataclass(frozen=True)
class SUTAdapter:
    name: str
    display_name: str

    base_url: str
    test_endpoint_path: str
    docs_path: str = "/docs"
    sut_ready_timeout: float = 5.0

    # Onboarding evidence shown verbatim to the Driver and rendered in the report.
    api_schema_doc: str = ""
    onboarding_extra: dict[str, Any] = field(default_factory=dict)
    happy_day_request: dict[str, Any] = field(default_factory=dict)

    # Casting (test-proposal) contract - the genuinely per-SUT part.
    casting_tool_schema: dict | None = None
    casting_system_prompt: Callable[[int, bool], str] | None = None
    validate_casting_response: Callable[[Any], list[str]] | None = None
    casting_max_tokens: Callable[[int], int] = _DEFAULT_CASTING_MAX_TOKENS

    # Execution.
    execute_test: Callable[[dict, int], dict] | None = None

    # Optional hooks; None falls back to an engine-generic default.
    redact_history_for_model: Callable[[list[dict]], list[dict]] | None = None
    describe_test_for_log: Callable[[dict], str] | None = None
    describe_result_for_log: Callable[[dict], str] | None = None

    # Report rendering hooks - the adapter owns request/response-shape rendering.
    render_test_entry: Callable[[dict], str] | None = None
    render_onboarding_section: Callable[[str, dict, dict], str] | None = None
    report_title: str | None = None

    # Suggested run-level defaults; RunConfig/CLI flags may override.
    default_max_checkpoints: int = 4
    default_first_round_test_budget: int = 12
    default_test_budget: int = 8


_REQUIRED_FIELDS = (
    "name", "display_name", "base_url", "test_endpoint_path",
    "casting_tool_schema", "casting_system_prompt", "validate_casting_response",
    "execute_test", "render_test_entry", "render_onboarding_section",
)


def validate_adapter(adapter: SUTAdapter) -> None:
    missing = [f for f in _REQUIRED_FIELDS if getattr(adapter, f) in (None, "")]
    if missing:
        raise ValueError(f"Adapter '{adapter.name}' is missing required field(s): {', '.join(missing)}")
