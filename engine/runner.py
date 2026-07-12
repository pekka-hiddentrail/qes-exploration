"""Orchestrates one full run against an adapter: SUT readiness probe,
happy-day fetch, the checkpoint loop, bug-report writing, and result files.
No domain knowledge lives here - everything SUT-specific comes from the
adapter."""

import itertools
import json

import httpx

from engine.adapter import SUTAdapter, validate_adapter
from engine.client import build_client
from engine.config import RunConfig
from engine.loop import get_bug_reports, get_happy_day_example, run_checkpoint_loop
from engine.report import render_report


def run(adapter: SUTAdapter, run_config: RunConfig) -> dict:
    validate_adapter(adapter)
    client = build_client()

    docs_url = adapter.base_url + adapter.docs_path
    try:
        httpx.get(docs_url, timeout=adapter.sut_ready_timeout)
    except httpx.TransportError:
        raise SystemExit(f"{adapter.name}'s SUT isn't running at {adapter.base_url} - start it first.")

    print("Fetching the one happy-day example from the live SUT...")
    happy_day_example = get_happy_day_example(adapter)
    print(f"  {happy_day_example['request']['body']} -> {happy_day_example['response']['body']}")

    output = {
        "api_schema": adapter.api_schema_doc,
        "onboarding_extra": adapter.onboarding_extra,
        "happy_day_example": happy_day_example,
    }
    bug_reports = []
    test_counter = itertools.count(1)

    try:
        casting_log, checkpoints, stopped_reason = run_checkpoint_loop(client, adapter, run_config, happy_day_example, test_counter)
        output["casting_log"] = casting_log
        output["checkpoints"] = checkpoints
        output["stopped_reason"] = stopped_reason

        final_hypothesis = checkpoints[-1]["hypothesis"]
        final_skeptic_review = checkpoints[-1]["skeptic_review"]
        anomalies = final_hypothesis.get("anomalies", [])
        output["anomaly_found"] = len(anomalies) > 0

        if anomalies:
            plural = "y" if len(anomalies) == 1 else "ies"
            print(f"Writing bug report(s) for {len(anomalies)} anomal{plural}...")
            bug_reports = get_bug_reports(
                client, adapter, run_config, final_hypothesis, final_skeptic_review, stopped_reason, casting_log
            )
    except RuntimeError as e:
        print(f"Stopped early: {e}")
        output["error"] = str(e)

    out_dir = run_config.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / "output.json"
    out_path.write_text(json.dumps(output, indent=2))
    print(f"\nWrote result to {out_path}")

    if bug_reports:
        bugs_path = out_dir / "bugs.json"
        bugs_path.write_text(json.dumps(bug_reports, indent=2))
        print(f"Wrote {len(bug_reports)} bug report(s) to {bugs_path}")

    report_path = out_dir / "report.html"
    report_path.write_text(render_report(output, bug_reports, adapter), encoding="utf-8")
    print(f"Wrote report to {report_path}")

    if "error" not in output:
        if output.get("anomaly_found"):
            print("Now score it by hand against rubric.md.")
        else:
            print("No anomaly found. See checkpoints for the final hypothesis and Skeptic's critique of it.")

    return output
