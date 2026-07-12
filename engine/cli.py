"""CLI entrypoint: python -m engine.cli --adapter <name> [options].

Generic infrastructure only - the Windows console UTF-8 workaround, argument
parsing, and wiring an adapter + RunConfig into engine.runner.run(). No
domain knowledge lives here.
"""

import argparse
import sys
from pathlib import Path

# Model-generated text (reasoning, probes) can contain non-ASCII characters that
# the default Windows console codec can't encode, crashing a plain print().
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.adapters.registry import available_adapters, load_adapter
from engine.config import RunConfig
from engine.runner import run


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI exploratory-testing engine against a SUT adapter.")
    parser.add_argument("--adapter", required=True, choices=available_adapters(), help="Which SUT adapter to run.")
    parser.add_argument("--model", default=None, help="Override the Anthropic model (default: engine default).")
    parser.add_argument("--max-checkpoints", type=int, default=None, dest="max_checkpoints")
    parser.add_argument("--first-round-budget", type=int, default=None, dest="first_round_test_budget")
    parser.add_argument("--default-budget", type=int, default=None, dest="default_test_budget")
    parser.add_argument("--out-dir", type=Path, default=None, help="Override the results directory (default: runs/<adapter>).")
    args = parser.parse_args()

    adapter = load_adapter(args.adapter)
    run_config = RunConfig.for_adapter(
        adapter,
        model=args.model,
        max_checkpoints=args.max_checkpoints,
        first_round_test_budget=args.first_round_test_budget,
        default_test_budget=args.default_test_budget,
        out_dir=args.out_dir,
    )
    run(adapter, run_config)


if __name__ == "__main__":
    main()
