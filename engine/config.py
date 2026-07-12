"""Run-level configuration. Layering: engine hardcoded fallback < adapter's
suggested defaults < explicit CLI overrides. Nothing here is domain-specific -
model choice and checkpoint/budget counts are run parameters, not SUT facts."""

from dataclasses import dataclass
from pathlib import Path

from engine.adapter import SUTAdapter
from engine.client import DEFAULT_MAX_ATTEMPTS, DEFAULT_MODEL


@dataclass(frozen=True)
class RunConfig:
    model: str = DEFAULT_MODEL
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_checkpoints: int = 4
    first_round_test_budget: int = 12
    default_test_budget: int = 8
    out_dir: Path = Path("runs/default")

    @staticmethod
    def for_adapter(
        adapter: SUTAdapter,
        *,
        model: str | None = None,
        max_attempts: int | None = None,
        max_checkpoints: int | None = None,
        first_round_test_budget: int | None = None,
        default_test_budget: int | None = None,
        out_dir: Path | None = None,
    ) -> "RunConfig":
        return RunConfig(
            model=model or DEFAULT_MODEL,
            max_attempts=max_attempts or DEFAULT_MAX_ATTEMPTS,
            max_checkpoints=max_checkpoints or adapter.default_max_checkpoints,
            first_round_test_budget=first_round_test_budget or adapter.default_first_round_test_budget,
            default_test_budget=default_test_budget or adapter.default_test_budget,
            out_dir=out_dir or Path("runs") / adapter.name,
        )
