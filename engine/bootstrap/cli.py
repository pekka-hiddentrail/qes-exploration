"""CLI entrypoint chaining all 4 adapter-bootstrap phases end-to-end:
python -m engine.bootstrap.cli --name <slug> --display-name <Name> --base-url <url>
[--spec-text <text>] [--max-probes N]

Writes a draft adapter under engine/adapters/<name>/ and prints the registry
line to add plus the run command - it deliberately does NOT edit
engine/adapters/registry.py itself. Registering (and thus running) a
generated adapter is the last explicit human gate before it goes live,
matching the "human review before trust" principle used throughout this
roadmap.
"""

import argparse
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from engine.bootstrap.generate import generate_adapter_source, write_adapter_module
from engine.bootstrap.probe import run_bootstrap_probe_loop
from engine.bootstrap.schema import discover_or_draft_schema
from engine.client import build_client

_ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap a draft SUTAdapter from a live API, end to end.")
    parser.add_argument("--name", required=True, help="Adapter slug, e.g. 'my_api' (used as the module/package name).")
    parser.add_argument("--display-name", required=True, dest="display_name")
    parser.add_argument("--base-url", required=True, dest="base_url")
    parser.add_argument("--spec-text", default=None, dest="spec_text",
                         help="Free-text API description, used only if schema discovery finds nothing.")
    parser.add_argument("--max-probes", type=int, default=8, dest="max_probes")
    args = parser.parse_args()

    anthropic_client = build_client()

    print(f"[1/3] Discovering schema at {args.base_url} ...")
    schema = discover_or_draft_schema(args.base_url, args.spec_text, anthropic_client)
    if not schema.endpoints:
        raise SystemExit(
            f"No usable schema found or drafted (status: {schema.status}). "
            "Nothing to probe - try passing --spec-text, or check the base URL."
        )
    print(f"    schema source: {schema.source}, endpoint: {schema.endpoints[0].method} {schema.endpoints[0].path}")

    print(f"[2/3] Probing live SUT to confirm the schema (up to {args.max_probes} probes) ...")
    bootstrap_result = run_bootstrap_probe_loop(anthropic_client, args.base_url, schema, max_probes=args.max_probes)
    print(f"    bootstrap status: {bootstrap_result.status}")

    if bootstrap_result.status == "failed":
        raise SystemExit(
            f"Bootstrap probing never got a working example - nothing real to generate an adapter around.\n"
            f"Reasoning from the last review: {bootstrap_result.notes or '(none recorded)'}"
        )

    print("[3/3] Generating draft adapter ...")
    source = generate_adapter_source(args.name, args.display_name, args.base_url, bootstrap_result)
    adapter_path = write_adapter_module(_ADAPTERS_DIR, args.name, source)

    print(f"\nDraft adapter written to {adapter_path}")
    if bootstrap_result.status == "inconclusive":
        print("NOTE: bootstrap was inconclusive - review the warning comment at the top of the file before trusting it.")
    print("\nTo register it, add this line to engine/adapters/registry.py's _ADAPTERS dict:")
    print(f'    "{args.name}": "engine.adapters.{args.name}.adapter",')
    print(f"\nThen run it with:\n    python -m engine.cli --adapter {args.name}")


if __name__ == "__main__":
    main()
