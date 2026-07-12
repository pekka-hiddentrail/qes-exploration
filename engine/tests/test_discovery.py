"""engine.bootstrap.discovery.discover_schema - Phase 1 of the adapter
bootstrap roadmap. Primary confidence comes from real mock SUTs: if either
sut.py's Pydantic model ever changes shape, these tests break for a real
reason, matching test_sut_regression.py's existing philosophy. Synthetic
httpx.MockTransport cases cover the handful of edges neither real mock
happens to exercise (an enum field, candidate-ordering, the three failure
states, a $ref cycle)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.adapters.complex_sut.sut import app as complex_sut_app
from engine.adapters.token_purchase.sut import app as token_purchase_app
from engine.bootstrap.discovery import discover_schema


# --- Real mock SUTs: the primary confidence source ---

def test_discover_schema_finds_real_token_purchase_openapi():
    result = discover_schema("http://test", client=TestClient(token_purchase_app))
    assert result.status == "found"
    assert result.fetched_from == "/openapi.json"

    [endpoint] = result.endpoints
    assert endpoint.path == "/purchase"
    assert endpoint.method == "POST"
    assert {f.name for f in endpoint.request_fields} == {
        "auth_token", "card_number", "expiry_month", "expiry_year", "cvv", "credit_count",
    }
    assert all(f.required for f in endpoint.request_fields)
    types_by_name = {f.name: f.type for f in endpoint.request_fields}
    assert types_by_name["auth_token"] == "string"
    assert types_by_name["expiry_month"] == "integer"
    # No response_model declared on this endpoint - legitimately empty, not a parse failure.
    assert endpoint.response_fields == []


def test_discover_schema_finds_complex_sut_optional_field_with_default():
    result = discover_schema("http://test", client=TestClient(complex_sut_app))
    assert result.status == "found"

    [endpoint] = result.endpoints
    assert endpoint.path == "/submit"
    fields_by_name = {f.name: f for f in endpoint.request_fields}
    assert fields_by_name["client_id"].required is True
    assert fields_by_name["payload"].required is True

    priority = fields_by_name["priority"]
    assert priority.required is False
    assert priority.has_default is True
    assert priority.default == "normal"


# --- Synthetic edges neither real mock happens to exercise ---

def _client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _openapi_doc(request_schema: dict, components_schemas: dict | None = None) -> dict:
    return {
        "openapi": "3.1.0",
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
                },
            },
        },
        "components": {"schemas": components_schemas or {}},
    }


def test_discover_schema_reads_enum_field():
    doc = _openapi_doc({
        "type": "object",
        "properties": {"priority": {"type": "string", "enum": ["normal", "high"]}},
        "required": ["priority"],
    })

    def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=doc)
        return httpx.Response(404)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "found"
    [field] = result.endpoints[0].request_fields
    assert field.enum == ["normal", "high"]


def test_discover_schema_tries_candidates_in_order_until_one_succeeds():
    doc = _openapi_doc({"type": "object", "properties": {}})

    def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(404)
        if request.url.path == "/swagger.json":
            return httpx.Response(200, json=doc)
        return httpx.Response(404)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "found"
    assert result.fetched_from == "/swagger.json"


def test_discover_schema_not_found_when_nothing_looks_like_a_schema():
    def handler(request):
        return httpx.Response(404)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "not_found"
    assert result.fetched_from is None
    assert result.endpoints == []


def test_discover_schema_unreachable_when_every_candidate_fails_to_connect():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "unreachable"


def test_discover_schema_malformed_when_document_has_no_paths():
    def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.1.0"})
        return httpx.Response(404)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "malformed"
    assert result.error is not None


def test_discover_schema_handles_self_referential_ref_cycle_without_hanging():
    doc = _openapi_doc(
        {"$ref": "#/components/schemas/Node"},
        components_schemas={
            "Node": {
                "type": "object",
                "properties": {"next": {"$ref": "#/components/schemas/Node"}},
            },
        },
    )

    def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=doc)
        return httpx.Response(404)

    result = discover_schema("http://test", client=_client_for(handler))
    assert result.status == "found"
    [field] = result.endpoints[0].request_fields
    assert field.name == "next"
