"""Kalshi research query builder -- constructs targeted web search queries.

Implements the SOURCE-FIRST, WHITELISTED SEARCH PIPELINE:
  1. Site-restricted queries to primary authoritative sources
  2. Metric-specific and date-scoped queries
  3. Confirmation queries to secondary outlets
  4. Contrarian queries to surface opposing evidence

Ported from Polymarket bot (src/research/query_builder.py) and extended
for Kalshi categories: POLITICS, ECONOMICS, CRYPTO, SPORTS, WEATHER,
SCIENCE, CULTURE, FINANCE, LEGAL, TECH, OTHER.

Usage::

    from kalshi_agent.research.query_builder import build_kalshi_queries

    queries = build_kalshi_queries(
        ticker="KXFED-25DEC-T4.75",
        title="Will the Fed cut rates in December 2025?",
        category="ECONOMICS",
        researchability=92,
    )
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SearchQuery:
    """A search query with intent metadata."""
    query_text: str
    site_restriction: str = ""       # e.g. "site:federalreserve.gov" (empty = no restriction)
    query_type: str = "general"      # "primary" | "news" | "statistics" | "contrarian" | "confirmation"
    priority: int = 1                # 1 = highest


# ---------------------------------------------------------------------------
# Site-restricted primary sources per Kalshi category
# ---------------------------------------------------------------------------

_SITE_RESTRICTED: dict[str, list[str]] = {
    "ECONOMICS": [
        "site:federalreserve.gov",
        "site:bls.gov",
        "site:bea.gov",
        "site:fred.stlouisfed.org",
        "site:treasury.gov",
    ],
    "POLITICS": [
        "site:reuters.com",
        "site:apnews.com",
        "site:congress.gov",
        "site:fec.gov",
        "site:ballotpedia.org",
    ],
    "CRYPTO": [
        "site:coindesk.com",
        "site:coingecko.com",
        "site:blockchain.info",
        "site:defillama.com",
    ],
    "SPORTS": [
        "site:espn.com",
        "site:sports-reference.com",
    ],
    "WEATHER": [
        "site:weather.gov",
        "site:noaa.gov",
        "site:nhc.noaa.gov",
    ],
    "FINANCE": [
        "site:sec.gov",
        "site:wsj.com",
        "site:bloomberg.com",
        "site:yahoo.com/finance",
    ],
    "SCIENCE": [
        "site:fda.gov",
        "site:clinicaltrials.gov",
        "site:nasa.gov",
        "site:nature.com",
        "site:arxiv.org",
    ],
    "LEGAL": [
        "site:scotusblog.com",
        "site:ftc.gov",
        "site:doj.gov",
        "site:reuters.com",
    ],
    "TECH": [
        "site:techcrunch.com",
        "site:theverge.com",
        "site:arstechnica.com",
    ],
    "CULTURE": [],   # Low researchability -- no site restrictions
    "OTHER": [],
}

# Secondary confirmation sources (used in general news queries)
_NEWS_SOURCES: dict[str, list[str]] = {
    "ECONOMICS": ["Reuters", "Bloomberg", "Wall Street Journal"],
    "POLITICS":  ["Politico", "AP News", "Reuters"],
    "CRYPTO":    ["CoinDesk", "CryptoSlate", "The Block"],
    "SPORTS":    ["ESPN", "Yahoo Sports"],
    "WEATHER":   ["Weather Underground", "AccuWeather"],
    "FINANCE":   ["Bloomberg", "Reuters", "CNBC"],
    "SCIENCE":   ["STAT News", "Nature", "Science Magazine"],
    "LEGAL":     ["Reuters", "Law360", "AP News"],
    "TECH":      ["TechCrunch", "Wired", "The Verge"],
    "CULTURE":   [],
    "OTHER":     ["Reuters", "AP News"],
}


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def build_kalshi_queries(
    ticker: str = "",
    title: str = "",
    category: str = "",
    researchability: Optional[int] = None,
    max_queries: int = 8,
) -> List[SearchQuery]:
    """Generate targeted search queries for a Kalshi market.

    Args:
        ticker: Kalshi market ticker (used to build ticker-specific queries).
        title: Market title / question string.
        category: Classified category (e.g. "ECONOMICS", "POLITICS").
        researchability: 0-100 score from classifier. Controls query budget:
                         LOW  (<40)  -> max 2 queries
                         MED  (<70)  -> max 4 queries
                         HIGH (>=70) -> up to max_queries
        max_queries: Hard cap on queries returned (default 8).

    Returns:
        List of SearchQuery objects sorted by priority (highest first).
    """
    # Strip question marks; remove leading question words
    question = title.strip().rstrip("?") if title else ""
    core = re.sub(r"^(Will|Is|Does|Has|Are|Do|Can|Should)\s+", "", question, flags=re.I)
    if not core:
        core = question

    # Normalize category
    cat = (category or "OTHER").upper()

    # --- Tiered query budget ---
    if researchability is not None:
        if researchability < 40:
            max_queries = min(max_queries, 2)
        elif researchability < 70:
            max_queries = min(max_queries, 4)
        # else: full budget

    queries: List[SearchQuery] = []

    # --- 1. Site-restricted primary source queries (max 2) ---
    site_list = _SITE_RESTRICTED.get(cat, [])
    for site in site_list[:2]:
        queries.append(SearchQuery(
            query_text=f"{site} {core}",
            site_restriction=site,
            query_type="primary",
            priority=1,
        ))

    # --- 2. Exact metric / data release query ---
    queries.append(SearchQuery(
        query_text=f'"{core}" official data release 2026',
        site_restriction="",
        query_type="statistics",
        priority=1,
    ))

    # --- 3. Recent news query ---
    news_sources = _NEWS_SOURCES.get(cat, [])
    news_suffix = f" ({' OR '.join(news_sources[:2])})" if news_sources else ""
    queries.append(SearchQuery(
        query_text=f"{core} latest news 2026{news_suffix}",
        site_restriction="",
        query_type="news",
        priority=2,
    ))

    # --- 4. Probability / forecast context (medium+ budget) ---
    if max_queries > 4:
        queries.append(SearchQuery(
            query_text=f"{core} probability forecast prediction analysis 2026",
            site_restriction="",
            query_type="confirmation",
            priority=2,
        ))

    # --- 5. Contrarian / opposing view (high budget only) ---
    if max_queries > 5:
        queries.append(SearchQuery(
            query_text=f"{core} unlikely reasons against criticism counterargument",
            site_restriction="",
            query_type="contrarian",
            priority=3,
        ))

    # --- 6. Ticker-specific query (helps for very specific Kalshi markets) ---
    if max_queries > 6 and ticker:
        # Extract meaningful part of ticker (e.g. "KXFED-25DEC-T4.75" -> "Fed December 2025")
        ticker_clean = re.sub(r"KX[A-Z]+[-.]?", "", ticker.upper()).replace("-", " ").replace(".", " ").strip()
        if ticker_clean and ticker_clean.lower() not in core.lower():
            queries.append(SearchQuery(
                query_text=f"{ticker_clean} {core} 2026",
                site_restriction="",
                query_type="confirmation",
                priority=3,
            ))

    # --- 7. Category + core (full budget) ---
    if max_queries > 7 and cat not in ("OTHER", "CULTURE") and cat.lower() not in core.lower():
        queries.append(SearchQuery(
            query_text=f"{cat.lower()} {core} 2026",
            site_restriction="",
            query_type="confirmation",
            priority=3,
        ))

    # Sort by priority and trim to budget
    queries.sort(key=lambda q: q.priority)
    result = queries[:max_queries]

    log.info(
        "[research] query_builder: ticker=%s category=%s researchability=%s "
        "num_queries=%d query_types=%s",
        ticker, cat, researchability, len(result),
        [q.query_type for q in result],
    )
    return result
