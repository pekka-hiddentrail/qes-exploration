"""Ties Phase 1 (schema discovery) and Phase 2 (free-text fallback)
together: try discovery first - free, deterministic, no LLM - and only fall
back to interpreting free text if discovery genuinely found nothing.
Phase 3 (active probing to confirm a draft) and Phase 4 (adapter synthesis)
aren't built yet; this is as far as the roadmap goes today.
"""

import httpx

from engine.bootstrap.discovery import DiscoveredSchema, discover_schema
from engine.bootstrap.freetext import propose_schema_from_text


def discover_or_draft_schema(
    base_url: str,
    spec_text: str | None,
    anthropic_client,
    timeout: float = 5.0,
    http_client: httpx.Client | None = None,
) -> DiscoveredSchema:
    """Discovery first. Only calls the LLM (Phase 2) if discovery didn't
    find a real schema AND spec_text was actually given - a discovery
    failure with nothing to fall back to is returned as-is, honestly,
    rather than silently fabricating a draft from nothing."""
    discovered = discover_schema(base_url, timeout=timeout, client=http_client)
    if discovered.status == "found":
        return discovered
    if not spec_text:
        return discovered
    return propose_schema_from_text(anthropic_client, spec_text)
