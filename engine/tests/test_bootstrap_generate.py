"""engine.bootstrap.generate - Phase 4 of the adapter-bootstrap roadmap.
The most important test dynamically imports a real generated adapter.py and
proves it passes validate_adapter() - not just "looks plausible" but
genuinely usable by the real engine."""

import importlib
import sys

import pytest

from engine.adapter import validate_adapter
from engine.bootstrap.discovery import DiscoveredEndpoint, DiscoveredField, DiscoveredSchema
from engine.bootstrap.generate import generate_adapter_source, write_adapter_module
from engine.bootstrap.probe import BootstrapResult


def _endpoint(fields=None):
    return DiscoveredEndpoint(
        path="/submit", method="POST",
        request_fields=fields if fields is not None else [
            DiscoveredField(name="client_id", type="string", required=True, description="identifies the caller"),
            DiscoveredField(name="payload", type="string", required=True, description="content to submit"),
            DiscoveredField(name="priority", type="string", required=False, enum=["normal", "high"]),
        ],
        response_fields=[], raw_request_schema={}, raw_response_schema={},
    )


def _confirmed_result(fields=None, notes="looks good"):
    endpoint = _endpoint(fields)
    schema = DiscoveredSchema(status="found", fetched_from=None, endpoints=[endpoint], source="freetext", confirmed=True)
    return BootstrapResult(
        status="confirmed",
        schema=schema,
        happy_day_example={
            "request": {"method": "POST", "path": "/submit", "body": {"client_id": "c1", "payload": "hi"}},
            "response": {"status": 200, "body": {"status": "accepted"}},
        },
        notes=notes,
    )


def _import_generated(tmp_path, monkeypatch, name, source):
    write_adapter_module(tmp_path, name, source)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(f"{name}.adapter", None)
    sys.modules.pop(name, None)
    importlib.invalidate_caches()
    return importlib.import_module(f"{name}.adapter")


def test_generated_adapter_passes_validate_adapter(tmp_path, monkeypatch):
    result = _confirmed_result()
    source = generate_adapter_source("gen_confirmed", "Gen Confirmed", "http://127.0.0.1:9000", result)

    module = _import_generated(tmp_path, monkeypatch, "gen_confirmed", source)

    validate_adapter(module.ADAPTER)  # raises on failure - the assertion IS "doesn't raise"
    assert module.ADAPTER.name == "gen_confirmed"
    assert module.ADAPTER.base_url == "http://127.0.0.1:9000"


def test_failed_bootstrap_result_raises_value_error():
    endpoint = _endpoint()
    schema = DiscoveredSchema(status="found", fetched_from=None, endpoints=[endpoint])
    failed = BootstrapResult(status="failed", schema=schema, happy_day_example=None, notes="never worked")

    with pytest.raises(ValueError, match="failed"):
        generate_adapter_source("gen_failed", "Gen Failed", "http://test", failed)


def test_inconclusive_bootstrap_result_still_generates_with_a_warning(tmp_path, monkeypatch):
    result = _confirmed_result(notes="still unsure whether priority actually does anything")
    inconclusive = BootstrapResult(
        status="inconclusive", schema=result.schema,
        happy_day_example=result.happy_day_example, notes=result.notes,
    )
    source = generate_adapter_source("gen_inconclusive", "Gen Inconclusive", "http://test", inconclusive)

    assert "DRAFT, NOT FULLY CONFIRMED" in source
    assert "still unsure whether priority actually does anything" in source

    module = _import_generated(tmp_path, monkeypatch, "gen_inconclusive", source)
    validate_adapter(module.ADAPTER)  # still a usable adapter, just a marked draft


def test_generated_validator_rejects_missing_required_fields(tmp_path, monkeypatch):
    module = _import_generated(
        tmp_path, monkeypatch, "gen_validator_missing",
        generate_adapter_source("gen_validator_missing", "Gen Validator Missing", "http://test", _confirmed_result()),
    )

    missing_required = {
        "linked_hypothesis": "", "client_id": "a", "priority": "normal",
        "predicted_outcome": "ok",
    }
    errors = module.validate_casting_response({"give_up": False, "reasoning": "r", "candidate_tests": [missing_required]})
    assert any("payload" in e for e in errors)
    assert any("predicted_status_family" in e for e in errors)
    assert not any("priority" in e and "missing" in e for e in errors)


def test_generated_validator_rejects_wrong_types_and_bad_enum(tmp_path, monkeypatch):
    module = _import_generated(
        tmp_path, monkeypatch, "gen_validator_types",
        generate_adapter_source("gen_validator_types", "Gen Validator Types", "http://test", _confirmed_result()),
    )

    well_formed = {
        "linked_hypothesis": "", "client_id": "a", "payload": "b", "priority": "normal",
        "predicted_outcome": "ok", "predicted_status_family": "2xx",
    }
    assert module.validate_casting_response(
        {"give_up": False, "reasoning": "r", "candidate_tests": [well_formed]}
    ) == []

    bad_type = dict(well_formed, client_id=123)
    errors = module.validate_casting_response({"give_up": False, "reasoning": "r", "candidate_tests": [bad_type]})
    assert any("client_id" in e for e in errors)

    bad_enum = dict(well_formed, priority="urgent")
    errors = module.validate_casting_response({"give_up": False, "reasoning": "r", "candidate_tests": [bad_enum]})
    assert any("priority" in e for e in errors)

    bad_status_family = dict(well_formed, predicted_status_family="3xx")
    errors = module.validate_casting_response({"give_up": False, "reasoning": "r", "candidate_tests": [bad_status_family]})
    assert any("predicted_status_family" in e for e in errors)


def test_generated_execute_test_includes_optional_fields_not_in_happy_day_example(tmp_path, monkeypatch):
    # HAPPY_DAY_REQUEST only has client_id/payload - priority must still be
    # forwarded when a test explicitly sets it, not silently dropped.
    import httpx

    from engine.http import call_sut_once

    module = _import_generated(
        tmp_path, monkeypatch, "gen_execute",
        generate_adapter_source("gen_execute", "Gen Execute", "http://test", _confirmed_result()),
    )

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "accepted"}))
    module.execute_test.__globals__["call_sut_once"] = (
        lambda base_url, path, body, method="POST": call_sut_once(base_url, path, body, transport=transport, method=method)
    )

    test = {
        "linked_hypothesis": "", "client_id": "x", "payload": "y", "priority": "high",
        "predicted_outcome": "ok", "predicted_status_family": "2xx",
    }
    entry = module.execute_test(test, 1)

    assert entry["request"]["body"] == {"client_id": "x", "payload": "y", "priority": "high"}
    assert entry["actual_status_family"] == "2xx"
    assert entry["prediction_matched"] is True


def test_render_test_entry_and_onboarding_section_produce_html(tmp_path, monkeypatch):
    module = _import_generated(
        tmp_path, monkeypatch, "gen_render",
        generate_adapter_source("gen_render", "Gen Render", "http://test", _confirmed_result()),
    )

    entry = {
        "test_number": 1, "linked_hypothesis": "maybe priority is ignored",
        "request": {"body": {"client_id": "a", "priority": "high"}},
        "predicted_outcome": "should be accepted", "predicted_status_family": "2xx",
        "actual_status_family": "2xx", "prediction_matched": True,
    }
    html = module.render_test_entry(entry)
    assert "maybe priority is ignored" in html
    assert "Test #1" in html

    onboarding = module.render_onboarding_section(
        module.ADAPTER.api_schema_doc, {}, {"request": {"body": {"client_id": "c1"}}, "response": {"body": {"status": "accepted"}}}
    )
    assert "API schema" in onboarding
    assert "Happy-day example" in onboarding
