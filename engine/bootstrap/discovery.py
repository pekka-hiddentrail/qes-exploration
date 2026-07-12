"""Schema discovery: the "bolt on, don't build" half of the adapter-bootstrap
roadmap. Most real APIs built with common frameworks (FastAPI, Spring +
springdoc, NestJS + Swagger, Django + drf-spectacular) already expose a
machine-readable OpenAPI/Swagger document for free - both existing mock SUTs
in this repo are FastAPI apps and already have one sitting at /openapi.json,
unused. This module tries fetching it before anything else in the bootstrap
roadmap (free-text interpretation, active probing) is attempted.

Deliberately hand-parses the narrow slice of OpenAPI actually needed
(request/response schema per path, $ref resolution against
components.schemas) rather than adding a dependency like prance or
openapi-spec-validator - both are built for problems this doesn't have
(arbitrary external/multi-file ref resolution; validating a whole spec
against the OpenAPI meta-schema), and would break the project's
deliberately narrow dependency footprint for a need two real, confirmed
examples fully specify.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

# Tried in order, stop at first that looks like a real schema document.
CANDIDATE_PATHS = ("/openapi.json", "/swagger.json", "/v3/api-docs", "/api/schema/")

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


@dataclass(frozen=True)
class DiscoveredField:
    name: str
    type: str  # JSON Schema type: string/integer/number/boolean/array/object/unknown
    required: bool
    enum: list[str] | None = None
    has_default: bool = False
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class DiscoveredEndpoint:
    path: str
    method: str
    request_fields: list[DiscoveredField]
    response_fields: list[DiscoveredField]  # legitimately [] with no declared response_model
    raw_request_schema: dict  # fully $ref-resolved JSON Schema fragment
    raw_response_schema: dict  # {} if undeclared


@dataclass(frozen=True)
class DiscoveredSchema:
    status: str  # "found" | "not_found" | "unreachable" | "malformed"
    fetched_from: str | None  # which candidate path succeeded, e.g. "/openapi.json"
    endpoints: list[DiscoveredEndpoint] = field(default_factory=list)
    error: str | None = None  # populated only when status == "malformed"


class _MalformedOpenAPIError(Exception):
    """A document looked like OpenAPI (had the right top-level key) but
    couldn't actually be extracted from - a bad $ref, no usable paths."""


def discover_schema(base_url: str, timeout: float = 5.0, client: httpx.Client | None = None) -> DiscoveredSchema:
    """Try each candidate path in turn. Distinguishes three honest failure
    states rather than collapsing them into one: not_found (some real HTTP
    response came back, none looked like a schema doc - the expected, common
    case), unreachable (every candidate failed at the connection level - a
    setup problem, not "this API doesn't publish docs"), and malformed
    (something claimed to be OpenAPI but couldn't actually be parsed).

    Accepts an optional pre-built client rather than a transport: FastAPI's
    TestClient (an httpx.Client subclass) is the correct way to hit an ASGI
    app synchronously in tests - a raw httpx.ASGITransport only implements
    the async transport interface and can't be used with a sync httpx.Client
    at all. Passing a whole client, not just a transport, lets tests supply
    a TestClient(app) directly.
    """
    owns_client = client is None
    if client is None:
        # Only applied to a client we build ourselves - a caller-provided
        # client (e.g. FastAPI's TestClient in tests) manages its own
        # timeout behavior, and TestClient specifically warns against
        # overriding it per-request.
        client = httpx.Client(timeout=timeout)

    got_any_http_response = False
    try:
        base = base_url.rstrip("/")
        for candidate in CANDIDATE_PATHS:
            try:
                response = client.get(f"{base}{candidate}", follow_redirects=True)
            except httpx.HTTPError:
                continue
            got_any_http_response = True

            if response.status_code != 200:
                continue
            try:
                document = response.json()
            except ValueError:
                continue
            # Without this check, a SPA fallback route or catch-all 200
            # handler at a coincidentally-matching path would be misread as
            # a schema document.
            if not isinstance(document, dict) or not ("openapi" in document or "swagger" in document):
                continue

            try:
                endpoints = _parse_openapi_document(document)
            except _MalformedOpenAPIError as e:
                return DiscoveredSchema(status="malformed", fetched_from=candidate, error=str(e))
            return DiscoveredSchema(status="found", fetched_from=candidate, endpoints=endpoints)
    finally:
        if owns_client:
            client.close()

    if got_any_http_response:
        return DiscoveredSchema(status="not_found", fetched_from=None)
    return DiscoveredSchema(status="unreachable", fetched_from=None)


def _parse_openapi_document(document: dict) -> list[DiscoveredEndpoint]:
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise _MalformedOpenAPIError("document has no usable 'paths'")

    components = document.get("components", {})
    components_schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    endpoints = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue

            request_schema = _extract_json_schema(operation.get("requestBody", {}))
            responses = operation.get("responses", {})
            response_schema = _extract_json_schema(responses.get("200", {}) if isinstance(responses, dict) else {})
            resolved_request = _resolve_refs(request_schema, components_schemas)
            resolved_response = _resolve_refs(response_schema, components_schemas)

            endpoints.append(DiscoveredEndpoint(
                path=path,
                method=method.upper(),
                request_fields=_fields_from_schema(resolved_request),
                response_fields=_fields_from_schema(resolved_response),
                raw_request_schema=resolved_request,
                raw_response_schema=resolved_response,
            ))

    if not endpoints:
        raise _MalformedOpenAPIError("no usable operations found in any path")
    return endpoints


def _extract_json_schema(container: dict) -> dict:
    if not isinstance(container, dict):
        return {}
    content = container.get("content", {})
    if not isinstance(content, dict):
        return {}
    json_content = content.get("application/json", {})
    schema = json_content.get("schema", {}) if isinstance(json_content, dict) else {}
    return schema if isinstance(schema, dict) else {}


def _resolve_refs(schema: dict, components_schemas: dict, seen: frozenset = frozenset()) -> dict:
    if not isinstance(schema, dict):
        return schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
            raise _MalformedOpenAPIError(f"unsupported $ref '{ref}' (only #/components/schemas/* supported)")
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            # A genuine self-reference cycle - stop expanding rather than
            # recursing forever. Treated as opaque, not an error: the rest
            # of the schema might still be perfectly usable.
            return {}
        target = components_schemas.get(name)
        if target is None:
            raise _MalformedOpenAPIError(f"$ref points at unknown schema '{name}'")
        return _resolve_refs(target, components_schemas, seen | {name})

    resolved = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            resolved[key] = {k: _resolve_refs(v, components_schemas, seen) for k, v in value.items()}
        elif key == "items":
            resolved[key] = _resolve_refs(value, components_schemas, seen)
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, list):
            resolved[key] = [_resolve_refs(v, components_schemas, seen) for v in value]
        else:
            resolved[key] = value
    return resolved


def _fields_from_schema(schema: dict) -> list[DiscoveredField]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return []
    required_list = schema.get("required", [])
    required = set(required_list) if isinstance(required_list, list) else set()
    fields = []
    for name, field_schema in properties.items():
        if not isinstance(field_schema, dict):
            continue
        fields.append(DiscoveredField(
            name=name,
            type=_field_type(field_schema),
            required=name in required,
            enum=_field_enum(field_schema),
            has_default="default" in field_schema,
            default=field_schema.get("default"),
            description=field_schema.get("description", ""),
        ))
    return fields


def _field_type(field_schema: dict) -> str:
    field_type = field_schema.get("type")
    if isinstance(field_type, list):
        # JSON Schema 2020-12 nullable form, e.g. ["string", "null"].
        non_null = [t for t in field_type if t != "null"]
        return non_null[0] if non_null else "unknown"
    if isinstance(field_type, str):
        return field_type

    # Optional[X] renders as anyOf: [{"type": "X"}, {"type": "null"}].
    for key in ("anyOf", "oneOf"):
        for branch in field_schema.get(key, []) or []:
            if isinstance(branch, dict) and branch.get("type") not in (None, "null"):
                return branch["type"]
    return "unknown"


def _field_enum(field_schema: dict) -> list[str] | None:
    if "enum" in field_schema:
        return list(field_schema["enum"])
    for key in ("anyOf", "oneOf"):
        for branch in field_schema.get(key, []) or []:
            if isinstance(branch, dict) and "enum" in branch:
                return list(branch["enum"])
    return None
