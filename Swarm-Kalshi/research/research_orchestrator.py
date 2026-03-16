"""Research orchestrator -- synchronous facade for the research/evidence pipeline.

Ties together:
  - query_builder  (generates search queries from market metadata)
  - web_search     (executes queries against search APIs)
  - source_fetcher (de-dups, ranks, fetches page content)
  - evidence_extractor (LLM extraction of structured evidence)

Public API
----------
  orchestrator = ResearchOrchestrator(config=cfg.get("research", {}))
  enrichment   = orchestrator.enrich_trade_request(signal)

Returns a dict with:
  research_summary       str   -- 2-3 sentence evidence landscape summary
  evidence_quality       float -- 0.0-1.0 blended quality score
  evidence_bullets       list  -- top evidence bullets as plain strings
  num_sources            int   -- number of sources found
  evidence_contradictions list -- contradiction descriptions

Always returns {} on any failure so it NEVER blocks trades.

Includes a 1-hour in-memory cache keyed by market title hash.
Hard 30-second timeout via asyncio.wait_for.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# ── 1-hour in-memory result cache ───────────────────────────────────

_CACHE_TTL_SECS = 3600  # 1 hour
_result_cache: dict[str, tuple[dict[str, Any], float]] = {}


def _cache_key(title: str) -> str:
    return hashlib.sha256(title.encode()).hexdigest()[:24]


def _cache_get(title: str) -> dict[str, Any] | None:
    key = _cache_key(title)
    entry = _result_cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() > expires_at:
        del _result_cache[key]
        return None
    return value


def _cache_put(title: str, value: dict[str, Any]) -> None:
    key = _cache_key(title)
    _result_cache[key] = (value, time.monotonic() + _CACHE_TTL_SECS)


# ── Core async pipeline ───────────────────────────────────────────────

async def _run_pipeline(
    title: str,
    market_category: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Execute the full research pipeline and return enrichment dict."""
    from research.query_builder import build_queries
    from research.web_search import create_search_provider
    from research.source_fetcher import SourceFetcher
    from research.evidence_extractor import EvidenceExtractor

    # Build search queries
    queries = build_queries(
        title=title,
        market_category=market_category,
        max_queries=int(config.get("max_queries", 6)),
    )

    if not queries:
        return {}

    # Create search provider
    search_provider_name = str(config.get("search_provider", "serpapi"))
    provider = create_search_provider(search_provider_name)

    try:
        fetcher = SourceFetcher(provider=provider, config=config)
        sources = await fetcher.fetch_sources(
            queries=queries,
            market_type=market_category.upper() if market_category else "UNKNOWN",
        )
        await fetcher.close()
    finally:
        await provider.close()

    if not sources:
        return {}

    # Extract evidence
    extractor = EvidenceExtractor(config=config)
    # Use title hash as a stable market_id stand-in
    market_id = hashlib.sha256(title.encode()).hexdigest()[:16]
    package = await extractor.extract(
        market_id=market_id,
        question=title,
        sources=sources,
        market_type=market_category.upper() if market_category else "UNKNOWN",
    )

    # Format evidence bullets as plain strings for the LLM prompt
    bullet_strings = [
        f"- {b.text} (source: {b.citation.publisher or b.citation.url}, "
        f"relevance={b.relevance:.2f})"
        for b in package.bullets[:8]  # cap at 8 bullets in the prompt
    ]

    contradiction_strings = [
        f"{c.claim_a} vs {c.claim_b} ({c.description})"
        for c in package.contradictions
    ]

    return {
        "research_summary": package.summary,
        "evidence_quality": package.quality_score,
        "evidence_bullets": bullet_strings,
        "num_sources": package.num_sources,
        "evidence_contradictions": contradiction_strings,
    }


# ── ResearchOrchestrator ──────────────────────────────────────────────

class ResearchOrchestrator:
    """Synchronous facade over the async research pipeline.

    Designed to be called from bot_runner.py's synchronous trade loop.
    Uses asyncio.run() internally.  A 30-second hard timeout prevents
    the research layer from ever stalling the trading loop.
    """

    _TIMEOUT_SECS = 30

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {}
        self._enabled = bool(self._config.get("enabled", False))

    def enrich_trade_request(self, signal: Any) -> dict[str, Any]:
        """Enrich a trade signal with web research evidence.

        Args:
            signal: Any object with .title and (optionally) .category attributes,
                    or a dict with the same keys.

        Returns:
            Dict with research_summary, evidence_quality, evidence_bullets,
            num_sources, evidence_contradictions.
            Returns {} gracefully on any failure.
        """
        if not self._enabled:
            return {}

        # Extract title and category from signal (object or dict)
        if isinstance(signal, dict):
            title = str(signal.get("title", "") or "")
            market_category = str(signal.get("category", "") or "")
        else:
            title = str(getattr(signal, "title", "") or "")
            market_category = str(getattr(signal, "category", "") or "")

        if not title:
            return {}

        # Check cache
        cached = _cache_get(title)
        if cached is not None:
            log.debug("research_orchestrator: cache hit title=%r", title[:60])
            return cached

        try:
            result = asyncio.run(
                asyncio.wait_for(
                    _run_pipeline(title, market_category, self._config),
                    timeout=self._TIMEOUT_SECS,
                )
            )
            if result:
                _cache_put(title, result)
            return result
        except asyncio.TimeoutError:
            log.warning(
                "research_orchestrator: timeout after %ds for title=%r",
                self._TIMEOUT_SECS, title[:60],
            )
            return {}
        except Exception as exc:
            log.warning(
                "research_orchestrator: pipeline failed title=%r error=%s",
                title[:60], str(exc),
            )
            return {}
