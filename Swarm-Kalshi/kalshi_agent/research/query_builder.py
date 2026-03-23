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
# Asset extraction helpers for price-point markets
# ---------------------------------------------------------------------------

# Maps Kalshi ticker prefix → (human name, symbol) for price markets
_CRYPTO_ASSETS: dict[str, tuple[str, str]] = {
    "KXBTC":   ("Bitcoin", "BTC"),
    "KXETH":   ("Ethereum", "ETH"),
    "KXSOL":   ("Solana", "SOL"),
    "KXBNB":   ("Binance Coin", "BNB"),
    "KXXRP":   ("XRP Ripple", "XRP"),
    "KXDOGE":  ("Dogecoin", "DOGE"),
    "KXADA":   ("Cardano", "ADA"),
    "KXAVAX":  ("Avalanche", "AVAX"),
}

def _extract_crypto_asset(ticker: str) -> Optional[tuple[str, str]]:
    """Return (human_name, symbol) if ticker is a known crypto price market."""
    upper = ticker.upper()
    for prefix, names in _CRYPTO_ASSETS.items():
        if upper.startswith(prefix):
            return names
    return None


def _extract_price_target(ticker: str) -> Optional[str]:
    """Extract the price threshold from a ticker like KXBTC-26MAR1711-B73875 -> '$73,875'."""
    m = re.search(r"-[BTA](\d+(?:\.\d+)?)", ticker)
    if m:
        val = float(m.group(1))
        if val > 999:
            return f"${val:,.0f}"
        return f"${val:g}"
    return None


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
    # CRYPTO: no site restrictions — price queries need broad sources
    # (specific asset queries are built by _build_crypto_queries below)
    "CRYPTO": [],
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

# ---------------------------------------------------------------------------
# Weather city lookup — maps Kalshi ticker city codes to searchable metadata
# ---------------------------------------------------------------------------

_WEATHER_CITIES: dict[str, tuple[str, str, str]] = {
    # code      city name           state  NWS station
    "DAL":   ("Dallas",            "TX",  "KDFW"),
    "ATL":   ("Atlanta",           "GA",  "KATL"),
    "DC":    ("Washington DC",     "DC",  "KDCA"),
    "LAX":   ("Los Angeles",       "CA",  "KLAX"),
    "OKC":   ("Oklahoma City",     "OK",  "KOKC"),
    "PHIL":  ("Philadelphia",      "PA",  "KPHL"),
    "BOS":   ("Boston",            "MA",  "KBOS"),
    "CHI":   ("Chicago",           "IL",  "KORD"),
    "NYC":   ("New York City",     "NY",  "KJFK"),
    "MIA":   ("Miami",             "FL",  "KMIA"),
    "DEN":   ("Denver",            "CO",  "KDEN"),
    "SEA":   ("Seattle",           "WA",  "KSEA"),
    "PHX":   ("Phoenix",           "AZ",  "KPHX"),
    "HOU":   ("Houston",           "TX",  "KIAH"),
    "SFO":   ("San Francisco",     "CA",  "KSFO"),
    "MIN":   ("Minneapolis",       "MN",  "KMSP"),
    "DET":   ("Detroit",           "MI",  "KDTW"),
    "CLV":   ("Cleveland",         "OH",  "KCLE"),
    "POR":   ("Portland",          "OR",  "KPDX"),
    "LAS":   ("Las Vegas",         "NV",  "KLAS"),
    "SAN":   ("San Antonio",       "TX",  "KSAT"),
    "AUS":   ("Austin",            "TX",  "KAUS"),
    "MEM":   ("Memphis",           "TN",  "KMEM"),
    "NAS":   ("Nashville",         "TN",  "KBNA"),
    "JAX":   ("Jacksonville",      "FL",  "KJAX"),
    "IND":   ("Indianapolis",      "IN",  "KIND"),
    "COL":   ("Columbus",          "OH",  "KCMH"),
    "SAC":   ("Sacramento",        "CA",  "KSMF"),
    "SLC":   ("Salt Lake City",    "UT",  "KSLC"),
}


def _parse_weather_ticker(ticker: str) -> Optional[tuple[str, str, str, str]]:
    """Extract (city_name, state, nws_station, date_str) from a weather ticker.

    Supports formats like:
      KXHIGHTDAL-26MAR23-T73   → Dallas, TX, KDFW, March 23
      KXHIGHTDC-26MAR23-B71.5  → Washington DC, DC, KDCA, March 23
    Returns None if city code is not recognised.
    """
    upper = ticker.upper()
    # Strip the KXHIGHT prefix to get CITY_CODE-DATE-THRESHOLD
    # Ticker format: KXHIGHT{CITY}-{HOUR}{MON}{DAY}-{THRESHOLD}
    # e.g. KXHIGHTDAL-26MAR23-T73 → city=DAL, hour=26, month=MAR, day=23
    m = re.match(r"KXHIGHT([A-Z]+)-(\d{2})([A-Z]{3})(\d{2})", upper)
    if not m:
        return None
    city_code, _hour, mon_abbr, day = m.group(1), m.group(2), m.group(3), m.group(4)
    info = _WEATHER_CITIES.get(city_code)
    if not info:
        return None
    city_name, state, station = info
    _MONTHS = {"JAN":"January","FEB":"February","MAR":"March","APR":"April",
               "MAY":"May","JUN":"June","JUL":"July","AUG":"August",
               "SEP":"September","OCT":"October","NOV":"November","DEC":"December"}
    from datetime import datetime
    year = datetime.utcnow().year
    month_name = _MONTHS.get(mon_abbr, mon_abbr)
    date_str = f"{month_name} {int(day)} {year}"
    return city_name, state, station, date_str


def _build_weather_queries(ticker: str, max_queries: int) -> Optional[List[SearchQuery]]:
    """Build forecast-focused queries for Kalshi weather temperature markets.

    The generic title-based approach fails for weather because queries like
    "the maximum temperature be 71-72° on Mar 23" search for recorded outcomes
    that don't exist yet. Instead we search for current NWS forecasts and
    hourly observations so the evidence extractor can compare against threshold.
    Returns None if ticker is not a recognised weather market.
    """
    parsed = _parse_weather_ticker(ticker)
    if not parsed:
        return None
    city, state, station, date_str = parsed

    queries = [
        SearchQuery(
            query_text=f"{city} {state} high temperature forecast {date_str}",
            site_restriction="",
            query_type="primary",
            priority=1,
        ),
        SearchQuery(
            query_text=f"site:forecast.weather.gov {city} {state} high temperature today",
            site_restriction="site:forecast.weather.gov",
            query_type="primary",
            priority=1,
        ),
        SearchQuery(
            query_text=f"{city} weather today high temperature NWS {date_str}",
            site_restriction="",
            query_type="statistics",
            priority=2,
        ),
        SearchQuery(
            query_text=f"{station} {city} airport weather observation high temperature {date_str}",
            site_restriction="",
            query_type="statistics",
            priority=2,
        ),
    ]
    return queries[:max_queries]


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
# Category-specific query overrides
# ---------------------------------------------------------------------------

def _build_crypto_queries(ticker: str, max_queries: int) -> Optional[List[SearchQuery]]:
    """Build price-focused queries for crypto intraday markets.

    For KXBTC-26MAR1711-B73875, we don't search for the exact title phrase —
    we search for the current BTC price so the evidence extractor can compare
    it to the threshold and produce a real quality/probability estimate.
    Returns None if ticker is not a recognised crypto price market.
    """
    asset = _extract_crypto_asset(ticker)
    if not asset:
        return None
    name, symbol = asset
    price_target = _extract_price_target(ticker)
    target_str = f" {price_target}" if price_target else ""

    queries = [
        SearchQuery(
            query_text=f"{name} {symbol} price today USD 2026",
            site_restriction="",
            query_type="primary",
            priority=1,
        ),
        SearchQuery(
            query_text=f"{symbol} USD current price live March 2026",
            site_restriction="",
            query_type="primary",
            priority=1,
        ),
        SearchQuery(
            query_text=f"site:coingecko.com {name} price",
            site_restriction="site:coingecko.com",
            query_type="statistics",
            priority=1,
        ),
        SearchQuery(
            query_text=f"{name} price prediction{target_str} March 17 2026",
            site_restriction="",
            query_type="news",
            priority=2,
        ),
    ]
    return queries[:max_queries]


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

    # --- Category-specific overrides (bypass generic title-based queries) ---
    if cat == "CRYPTO" and ticker:
        crypto_queries = _build_crypto_queries(ticker, max_queries)
        if crypto_queries:
            log.info(
                "[research] query_builder: ticker=%s category=%s researchability=%s "
                "num_queries=%d query_types=%s",
                ticker, cat, researchability, len(crypto_queries),
                [q.query_type for q in crypto_queries],
            )
            return crypto_queries

    if cat == "WEATHER" and ticker:
        weather_queries = _build_weather_queries(ticker, max_queries)
        if weather_queries:
            log.info(
                "[research] query_builder: ticker=%s category=%s researchability=%s "
                "num_queries=%d query_types=%s",
                ticker, cat, researchability, len(weather_queries),
                [q.query_type for q in weather_queries],
            )
            return weather_queries

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
