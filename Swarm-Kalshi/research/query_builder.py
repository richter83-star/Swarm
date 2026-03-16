"""Query builder -- constructs targeted web search queries from market data.

Implements the SOURCE-FIRST, WHITELISTED SEARCH PIPELINE:
  1. Site-restricted queries to primary authoritative sources
  2. Metric-specific and date-scoped queries
  3. Confirmation queries to secondary outlets
  4. Contrarian queries to surface opposing evidence

Ported from Polymarket bot -- src/research/query_builder.py.
Accepts title:str + market_category:str instead of a GammaMarket object.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# ── Kalshi category normalization ─────────────────────────────────────
# Maps Kalshi category strings (lower-case) to the canonical set used
# for site-restricted source lookups below.
_CATEGORY_MAP: dict[str, str] = {
    # politics / elections
    "politics": "ELECTION",
    "election": "ELECTION",
    "elections": "ELECTION",
    "political": "ELECTION",
    "government": "ELECTION",
    # economics / macro
    "economics": "MACRO",
    "economic": "MACRO",
    "macro": "MACRO",
    "finance": "MACRO",
    "financial": "MACRO",
    "economy": "MACRO",
    "fed": "MACRO",
    "interest_rate": "MACRO",
    # corporate / equities
    "corporate": "CORPORATE",
    "business": "CORPORATE",
    "earnings": "CORPORATE",
    "stocks": "CORPORATE",
    "equity": "CORPORATE",
    # weather / climate
    "weather": "WEATHER",
    "climate": "WEATHER",
    "hurricane": "WEATHER",
    "storm": "WEATHER",
    # science / tech
    "science": "SCIENCE",
    "technology": "SCIENCE",
    "tech": "SCIENCE",
    "research": "SCIENCE",
    # regulation / law
    "regulation": "REGULATION",
    "law": "REGULATION",
    "legal": "REGULATION",
    "regulatory": "REGULATION",
    # geopolitics
    "geopolitics": "GEOPOLITICS",
    "foreign_policy": "GEOPOLITICS",
    "international": "GEOPOLITICS",
    "war": "GEOPOLITICS",
    "conflict": "GEOPOLITICS",
    # crypto
    "crypto": "CRYPTO",
    "cryptocurrency": "CRYPTO",
    "bitcoin": "CRYPTO",
    "defi": "CRYPTO",
    # sports
    "sports": "SPORTS",
    "sport": "SPORTS",
    # entertainment
    "entertainment": "ENTERTAINMENT",
    "culture": "ENTERTAINMENT",
    "media": "ENTERTAINMENT",
}


def normalize_kalshi_category(raw_category: str) -> str:
    """Map a raw Kalshi category string to a canonical research category."""
    key = raw_category.strip().lower().replace(" ", "_").replace("-", "_")
    return _CATEGORY_MAP.get(key, "")


# ── Primary source sites by category ─────────────────────────────────
_SITE_RESTRICTED: dict[str, list[str]] = {
    "MACRO": [
        "site:bls.gov",
        "site:bea.gov",
        "site:federalreserve.gov",
        "site:fred.stlouisfed.org",
        "site:treasury.gov",
    ],
    "ELECTION": [
        "site:fec.gov",
        "site:ballotpedia.org",
    ],
    "CORPORATE": [
        "site:sec.gov",
    ],
    "WEATHER": [
        "site:noaa.gov",
        "site:nhc.noaa.gov",
        "site:weather.gov",
    ],
    "SCIENCE": [
        "site:nature.com",
        "site:science.org",
        "site:arxiv.org",
    ],
    "REGULATION": [
        "site:sec.gov",
        "site:federalregister.gov",
        "site:congress.gov",
    ],
    "GEOPOLITICS": [
        "site:un.org",
        "site:state.gov",
    ],
    "CRYPTO": [
        "site:coindesk.com",
        "site:defillama.com",
    ],
    "SPORTS": [],
    "ENTERTAINMENT": [],
}


@dataclass
class SearchQuery:
    """A search query with intent metadata."""
    text: str
    intent: str  # "primary" | "news" | "statistics" | "contrarian" | "confirmation"
    priority: int = 1  # 1 = highest


def build_queries(
    title: str,
    market_category: str,
    max_queries: int = 8,
    researchability: Optional[int] = None,
) -> list[SearchQuery]:
    """Generate search queries for a Kalshi market.

    Args:
        title: Market title / question string.
        market_category: Raw Kalshi category (will be normalized internally).
        max_queries: Hard cap on queries returned.
        researchability: 0-100 score.  Controls budget:
                         LOW (<40)  -> max 2 queries
                         NORMAL     -> max 4 queries
                         HIGH (>=70) -> up to max_queries
    """
    question = title.strip().rstrip("?")
    core = re.sub(r"^(Will|Is|Does|Has|Are|Do|Can|Should)\s+", "", question, flags=re.I)

    # Normalize category
    canonical_category = normalize_kalshi_category(market_category)

    # Tiered budget based on researchability
    if researchability is not None:
        if researchability < 40:
            max_queries = min(max_queries, 2)
        elif researchability < 70:
            max_queries = min(max_queries, 4)

    queries: list[SearchQuery] = []

    # 1. Site-restricted primary source queries
    site_restrictions = _SITE_RESTRICTED.get(canonical_category, [])
    for site in site_restrictions[:2]:
        queries.append(SearchQuery(
            text=f"{site} {core}",
            intent="primary",
            priority=1,
        ))

    # 2. Exact metric search
    queries.append(SearchQuery(
        text=f'"{core}" official data release 2026',
        intent="statistics",
        priority=1,
    ))

    # 3. Recent news from major outlets
    queries.append(SearchQuery(
        text=f"{core} latest news 2026",
        intent="news",
        priority=2,
    ))

    # 4. Probability / forecast context
    if max_queries > 4:
        queries.append(SearchQuery(
            text=f"{core} probability forecast prediction analysis",
            intent="confirmation",
            priority=2,
        ))

    # 5. Contrarian / opposing view (only for high-budget)
    if max_queries > 5:
        queries.append(SearchQuery(
            text=f"{core} unlikely reasons against criticism",
            intent="contrarian",
            priority=3,
        ))

    # 6. Category-specific refinement (only for high-budget)
    if max_queries > 6 and market_category and market_category.lower() not in core.lower():
        queries.append(SearchQuery(
            text=f"{market_category} {core}",
            intent="confirmation",
            priority=3,
        ))

    # Sort by priority and trim
    queries.sort(key=lambda q: q.priority)
    result = queries[:max_queries]

    log.info(
        "query_builder: title=%r category=%s canonical=%s researchability=%s num_queries=%d intents=%s",
        title[:60], market_category, canonical_category, researchability,
        len(result), [q.intent for q in result],
    )
    return result
