"""Kalshi market classifier -- 11-category taxonomy with researchability scoring.

Ported and extended from Polymarket bot (src/engine/market_classifier.py).

Key differences from the Polymarket classifier:
  - Kalshi-specific ticker prefix detection (KXFED-*, KXBTCD-*, KXNCAA-*, etc.)
  - Mapped to 11 Kalshi categories: POLITICS, ECONOMICS, CRYPTO, SPORTS, WEATHER,
    SCIENCE, CULTURE, FINANCE, LEGAL, TECH, OTHER
  - Ticker prefix rules take precedence over text-match rules
  - Returns researchability_score, primary_sources, search_strategy, query_budget

Usage::

    from kalshi_agent.research.market_classifier import classify_kalshi_market

    result = classify_kalshi_market(
        ticker="KXFED-25DEC-T4.75",
        title="Will the Fed cut rates in December 2025?",
    )
    # result.category == "ECONOMICS"
    # result.researchability_score == 92
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class KalshiMarketClassification:
    """Rich classification result for a Kalshi market."""

    category: str                          # POLITICS | ECONOMICS | CRYPTO | SPORTS |
                                           # WEATHER | SCIENCE | CULTURE | FINANCE |
                                           # LEGAL | TECH | OTHER
    subcategory: str                       # e.g. "fed_rates", "btc_price"
    researchability_score: int             # 0-100
    researchability_reasons: List[str] = field(default_factory=list)
    primary_sources: List[str] = field(default_factory=list)
    search_strategy: str = ""              # "official_data" | "news_analysis" |
                                           # "market_data" | "sports_odds" | "skip"
    query_budget: int = 4                  # recommended number of queries (2-8)
    worth_researching: bool = True
    confidence: float = 0.8
    tags: List[str] = field(default_factory=list)
    ticker_matched: bool = False           # True when ticker prefix drove the decision

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "subcategory": self.subcategory,
            "researchability_score": self.researchability_score,
            "researchability_reasons": self.researchability_reasons,
            "primary_sources": self.primary_sources,
            "search_strategy": self.search_strategy,
            "query_budget": self.query_budget,
            "worth_researching": self.worth_researching,
            "confidence": self.confidence,
            "tags": self.tags,
            "ticker_matched": self.ticker_matched,
        }


# ---------------------------------------------------------------------------
# Ticker-prefix classification rules
# ---------------------------------------------------------------------------
# Format: (prefix, category, subcategory, config_dict)
# Ticker prefixes are matched case-insensitively against the start of the ticker.

_TickerRule = Tuple[str, str, str, Dict[str, Any]]

_TICKER_PREFIX_RULES: List[_TickerRule] = [
    # --- ECONOMICS ---
    ("KXFED",   "ECONOMICS", "fed_rates",
     dict(researchability=92, sources=["federalreserve.gov", "CME FedWatch", "Reuters"],
          strategy="official_data", queries=8, tags=["scheduled_event", "high_signal"],
          reasons=["Official Fed calendar provides exact meeting dates",
                   "CME FedWatch gives real-time market-implied probabilities"])),
    ("KXCPI",   "ECONOMICS", "inflation",
     dict(researchability=90, sources=["bls.gov", "Cleveland Fed Nowcast", "Reuters"],
          strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
          reasons=["BLS releases CPI on fixed schedule",
                   "Cleveland Fed Nowcast provides pre-release estimates"])),
    ("KXPCE",   "ECONOMICS", "pce_inflation",
     dict(researchability=89, sources=["bea.gov", "federalreserve.gov", "Reuters"],
          strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
          reasons=["BEA releases PCE on fixed schedule"])),
    ("KXPPI",   "ECONOMICS", "ppi",
     dict(researchability=87, sources=["bls.gov", "Reuters"],
          strategy="official_data", queries=6, tags=["scheduled_event", "data_release"],
          reasons=["BLS releases PPI on fixed schedule"])),
    ("KXJOB",   "ECONOMICS", "employment",
     dict(researchability=90, sources=["bls.gov", "ADP", "Reuters"],
          strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
          reasons=["BLS jobs report released first Friday each month"])),
    ("KXUNR",   "ECONOMICS", "unemployment",
     dict(researchability=90, sources=["bls.gov", "Reuters"],
          strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
          reasons=["BLS unemployment data on fixed schedule"])),
    ("KXGDP",   "ECONOMICS", "gdp",
     dict(researchability=88, sources=["bea.gov", "Atlanta Fed GDPNow", "Reuters"],
          strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
          reasons=["BEA advance/preliminary/final GDP reports on set schedule"])),
    ("KXRETAIL","ECONOMICS", "retail_sales",
     dict(researchability=85, sources=["census.gov", "Reuters"],
          strategy="official_data", queries=6, tags=["scheduled_event", "data_release"],
          reasons=["Census Bureau releases retail sales monthly"])),
    ("KXHIGHT", "WEATHER", "temperature",
     dict(researchability=82, sources=["weather.gov", "noaa.gov", "weather.com"],
          strategy="official_data", queries=4, tags=["daily_data", "measurable"],
          reasons=["NOAA/NWS provide hourly forecasts with high accuracy same-day",
                   "Historical climatology provides strong baselines"])),
    ("KXHIGHTMIN", "WEATHER", "temperature_min",
     dict(researchability=82, sources=["weather.gov", "noaa.gov", "weather.com"],
          strategy="official_data", queries=4, tags=["daily_data", "measurable"],
          reasons=["NOAA/NWS min temperature forecasts highly accurate same-day"])),
    ("KXHIGHTMAX", "WEATHER", "temperature_max",
     dict(researchability=82, sources=["weather.gov", "noaa.gov", "weather.com"],
          strategy="official_data", queries=4, tags=["daily_data", "measurable"],
          reasons=["NOAA/NWS max temperature forecasts highly accurate same-day"])),
    ("KXRAIN",  "WEATHER", "precipitation",
     dict(researchability=75, sources=["weather.gov", "noaa.gov"],
          strategy="official_data", queries=3, tags=["daily_data", "measurable"],
          reasons=["NWS precipitation forecasts available"])),
    ("KXSNOW",  "WEATHER", "snowfall",
     dict(researchability=72, sources=["weather.gov", "noaa.gov"],
          strategy="official_data", queries=3, tags=["daily_data", "measurable"],
          reasons=["NWS snowfall forecasts available"])),

    # --- FINANCE / CORPORATE ---
    ("KXSEC",   "FINANCE", "regulatory",
     dict(researchability=80, sources=["sec.gov", "Reuters"],
          strategy="official_data", queries=6, tags=["regulatory"],
          reasons=["SEC filings and orders are public"])),
    ("KXSTOCK", "FINANCE", "stock_price",
     dict(researchability=55, sources=["Yahoo Finance", "Bloomberg"],
          strategy="market_data", queries=4, tags=["volatile", "price_target"],
          reasons=["Stock prices are public but volatile"])),
    ("KXIPS",   "ECONOMICS", "industrial_production",
     dict(researchability=84, sources=["federalreserve.gov", "Reuters"],
          strategy="official_data", queries=6, tags=["scheduled_event", "data_release"],
          reasons=["Fed releases industrial production monthly"])),

    # --- CRYPTO ---
    ("KXBTCD",  "CRYPTO", "btc_price",
     dict(researchability=65, sources=["CoinGecko", "CoinDesk", "TradingView"],
          strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
          reasons=["Real-time price data available",
                   "High volatility makes predictions harder"])),
    ("KXETH",   "CRYPTO", "eth_price",
     dict(researchability=62, sources=["CoinGecko", "CoinDesk"],
          strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
          reasons=["Real-time price data available but very volatile"])),
    ("KXBTC",   "CRYPTO", "btc_general",
     dict(researchability=65, sources=["CoinGecko", "CoinDesk"],
          strategy="market_data", queries=5, tags=["volatile"],
          reasons=["Crypto markets data-rich but volatile"])),
    ("KXCRYPTO","CRYPTO", "general",
     dict(researchability=58, sources=["CoinDesk", "CoinGecko"],
          strategy="market_data", queries=4, tags=["volatile"],
          reasons=["Crypto markets data-rich but volatile"])),

    # --- SPORTS ---
    ("KXNCAA",  "SPORTS", "ncaa",
     dict(researchability=50, sources=["ESPN", "sports-reference.com"],
          strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
          reasons=["Sports odds available but outcomes highly unpredictable"])),
    ("KXNBA",   "SPORTS", "nba",
     dict(researchability=50, sources=["ESPN", "basketball-reference.com"],
          strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
          reasons=["Sports odds available but outcomes highly unpredictable"])),
    ("KXNFL",   "SPORTS", "nfl",
     dict(researchability=50, sources=["ESPN", "pro-football-reference.com"],
          strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
          reasons=["Sports odds available but outcomes highly unpredictable"])),
    ("KXMLB",   "SPORTS", "mlb",
     dict(researchability=50, sources=["ESPN", "baseball-reference.com"],
          strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
          reasons=["Sports odds available but outcomes highly unpredictable"])),
    ("KXNHL",   "SPORTS", "nhl",
     dict(researchability=45, sources=["ESPN", "hockey-reference.com"],
          strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
          reasons=["Sports odds available but outcomes highly unpredictable"])),
    ("KXSPORTS","SPORTS", "general",
     dict(researchability=45, sources=["ESPN"],
          strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
          reasons=["Sports outcomes are hard to predict without domain expertise"])),

    # --- POLITICS ---
    ("KXELECT", "POLITICS", "election",
     dict(researchability=88, sources=["FiveThirtyEight", "RCP", "AP News"],
          strategy="news_analysis", queries=8, tags=["polling_data", "high_signal"],
          reasons=["Extensive polling data available",
                   "Major news coverage from multiple outlets"])),
    ("KXPRES",  "POLITICS", "presidential",
     dict(researchability=85, sources=["AP News", "Reuters", "Politico"],
          strategy="news_analysis", queries=7, tags=["polling_data"],
          reasons=["Presidential markets widely covered"])),
    ("KXPOL",   "POLITICS", "general",
     dict(researchability=78, sources=["AP News", "Reuters", "Politico"],
          strategy="news_analysis", queries=6, tags=["political"],
          reasons=["Political markets widely covered by wire services"])),
    ("KXEOWEEK","POLITICS", "executive_order",
     dict(researchability=70, sources=["federalregister.gov", "AP News", "Reuters"],
          strategy="news_analysis", queries=5, tags=["executive_action"],
          reasons=["Executive orders tracked in Federal Register"])),
    ("KXLAGODAYS","POLITICS", "political_action",
     dict(researchability=68, sources=["AP News", "Reuters", "Politico"],
          strategy="news_analysis", queries=5, tags=["political"],
          reasons=["Political actions covered by wire services"])),
    ("KXDHSFUNDING","POLITICS", "legislation",
     dict(researchability=72, sources=["congress.gov", "Politico", "Reuters"],
          strategy="news_analysis", queries=6, tags=["legislative_tracking"],
          reasons=["Congress.gov tracks bill status"])),

    # --- WEATHER ---
    ("KXHURRICANE","WEATHER", "hurricane",
     dict(researchability=72, sources=["NOAA", "NHC", "Weather.gov"],
          strategy="official_data", queries=5, tags=["time_sensitive", "nowcast"],
          reasons=["NOAA provides excellent tracking data for active storms"])),
    ("KXWEATHER", "WEATHER", "forecast",
     dict(researchability=56, sources=["NOAA", "Weather.gov", "AccuWeather"],
          strategy="official_data", queries=4, tags=["nowcast"],
          reasons=["Weather forecasts degrade beyond 7-10 days"])),
    ("KXTEMP",  "WEATHER", "temperature",
     dict(researchability=60, sources=["NOAA", "Weather.gov"],
          strategy="official_data", queries=4, tags=["nowcast"],
          reasons=["Temperature records publicly available from NOAA"])),

    # --- SCIENCE ---
    ("KXFDA",   "SCIENCE", "pharma",
     dict(researchability=82, sources=["FDA.gov", "ClinicalTrials.gov", "STAT News"],
          strategy="official_data", queries=6, tags=["scheduled_event", "regulatory"],
          reasons=["PDUFA dates are scheduled in advance",
                   "Clinical trial data on ClinicalTrials.gov"])),
    ("KXNASA",  "SCIENCE", "space",
     dict(researchability=80, sources=["NASA.gov", "SpaceX", "Space.com"],
          strategy="news_analysis", queries=5, tags=["scheduled_event"],
          reasons=["Launch windows are publicly scheduled"])),

    # --- TECH ---
    ("KXAI",    "TECH", "ai",
     dict(researchability=62, sources=["TechCrunch", "The Verge", "ArXiv"],
          strategy="news_analysis", queries=5, tags=["fast_moving"],
          reasons=["AI news moves fast -- hard to predict specifics"])),
    ("KXTECH",  "TECH", "general",
     dict(researchability=60, sources=["TechCrunch", "The Verge"],
          strategy="news_analysis", queries=4, tags=["fast_moving"],
          reasons=["Tech news generally well-covered"])),

    # --- LEGAL ---
    ("KXLEGAL", "LEGAL", "court_cases",
     dict(researchability=78, sources=["SCOTUS Blog", "Reuters", "AP News"],
          strategy="news_analysis", queries=6, tags=["legal_proceeding"],
          reasons=["Court calendars and filings are public"])),
    ("KXANTITRUST","LEGAL", "antitrust",
     dict(researchability=76, sources=["FTC.gov", "DOJ.gov", "Reuters"],
          strategy="official_data", queries=6, tags=["regulatory"],
          reasons=["Antitrust filings and decisions are public"])),
]

# Build a lookup dict: lowercase prefix -> rule tuple
_TICKER_LOOKUP: Dict[str, Tuple[str, str, Dict[str, Any]]] = {
    prefix.lower(): (cat, sub, cfg)
    for prefix, cat, sub, cfg in _TICKER_PREFIX_RULES
}


# ---------------------------------------------------------------------------
# Text-match classification rules (fallback when ticker gives no match)
# ---------------------------------------------------------------------------
# Each rule: (compiled_regex, category, subcategory, config_dict)

_TextRule = Tuple[re.Pattern, str, str, Dict[str, Any]]

_TEXT_RULES: List[_TextRule] = []


def _r(pattern: str, cat: str, sub: str, **kw: Any) -> None:
    """Register a text-match classification rule."""
    _TEXT_RULES.append((re.compile(pattern, re.IGNORECASE), cat, sub, kw))


# ECONOMICS
_r(r"\b(fed(eral reserve)?|fomc|interest\s+rate|rate\s+(cut|hike|hold|pause|decision)|fed\s+fund)\b",
   "ECONOMICS", "fed_rates",
   researchability=92, sources=["federalreserve.gov", "CME FedWatch", "Reuters"],
   strategy="official_data", queries=8, tags=["scheduled_event", "high_signal"],
   reasons=["Official Fed calendar; CME FedWatch real-time probabilities"])

_r(r"\b(cpi|inflation|consumer\s+price|price\s+index|pce|core\s+inflation|ppi)\b",
   "ECONOMICS", "inflation",
   researchability=90, sources=["bls.gov", "bea.gov", "Reuters"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BLS/BEA release on fixed schedule"])

_r(r"\b(gdp|gross\s+domestic|economic\s+growth)\b",
   "ECONOMICS", "gdp",
   researchability=88, sources=["bea.gov", "Atlanta Fed GDPNow"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BEA GDP on set schedule"])

_r(r"\b(unemployment|jobless|nonfarm\s+payroll|payrolls?|jobs?\s+report|employment)\b",
   "ECONOMICS", "employment",
   researchability=90, sources=["bls.gov", "ADP"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["BLS jobs report on first Friday of each month"])

_r(r"\b(tariff|trade\s+(war|deal|deficit)|import\s+dut|export\s+ban|trade\s+agreement)\b",
   "ECONOMICS", "trade",
   researchability=78, sources=["ustr.gov", "Reuters", "Bloomberg"],
   strategy="news_analysis", queries=6, tags=["policy_dependent"],
   reasons=["Trade policy politically driven -- news coverage is good"])

_r(r"\b(treasury|bond\s+yield|yield\s+curve|10.year|2.year|t.bill|debt\s+ceiling)\b",
   "ECONOMICS", "bonds",
   researchability=85, sources=["treasury.gov", "Bloomberg", "FRED"],
   strategy="official_data", queries=6, tags=["real_time_data"],
   reasons=["Treasury yields are publicly available real-time"])

_r(r"\b(recession|economic\s+downturn|soft\s+landing|hard\s+landing)\b",
   "ECONOMICS", "recession",
   researchability=75, sources=["NBER", "Federal Reserve", "Reuters"],
   strategy="news_analysis", queries=6, tags=["long_horizon"],
   reasons=["Recession declared retrospectively by NBER; leading indicators imperfect"])

# POLITICS
_r(r"\b(president(ial)?|white\s+house)\b.{0,40}\b(win|elect|nominee|race)\b|\b(win|elect|nominee|race)\b.{0,40}\b(president(ial)?|white\s+house)\b",
   "POLITICS", "presidential",
   researchability=88, sources=["FiveThirtyEight", "RCP", "AP News"],
   strategy="news_analysis", queries=8, tags=["polling_data", "high_signal"],
   reasons=["Extensive polling data available"])

_r(r"\b(senate|senat|congress(ional)?|house\s+(of\s+)?rep|midterm)\b",
   "POLITICS", "congressional",
   researchability=82, sources=["Cook Political Report", "FiveThirtyEight", "Ballotpedia"],
   strategy="news_analysis", queries=6, tags=["polling_data"],
   reasons=["Good polling and historical data for most races"])

_r(r"\b(executive\s+order|cabinet|appoint(ment|ed)?|nomin(ate|ation|ee)|confirm(ation)?|secretary\s+of)\b",
   "POLITICS", "appointments",
   researchability=72, sources=["AP News", "Reuters", "Politico"],
   strategy="news_analysis", queries=5, tags=["political"],
   reasons=["Appointments are covered by political press"])

_r(r"\b(bill\s+pass|legislat|act\s+(pass|sign|vote)|law\s+(pass|sign)|funding)\b",
   "POLITICS", "legislation",
   researchability=70, sources=["congress.gov", "Politico", "Reuters"],
   strategy="news_analysis", queries=5, tags=["legislative_tracking"],
   reasons=["Congress.gov tracks bill status"])

_r(r"\b(election|vote|ballot|poll(ing)?|primary|caucus|electoral)\b",
   "POLITICS", "general",
   researchability=80, sources=["AP News", "Reuters", "FiveThirtyEight"],
   strategy="news_analysis", queries=6, tags=["polling_data"],
   reasons=["General election coverage widely available"])

# CRYPTO
_r(r"\b(bitcoin|btc)\b.{0,30}\b(price|reach|hit|above|below|\$|usd)\b",
   "CRYPTO", "btc_price",
   researchability=65, sources=["CoinGecko", "TradingView", "CoinDesk"],
   strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
   reasons=["Real-time price data available; high volatility"])

_r(r"\b(ethereum|eth)\b.{0,30}\b(price|reach|hit|above|below|\$|usd)\b",
   "CRYPTO", "eth_price",
   researchability=62, sources=["CoinGecko", "TradingView", "CoinDesk"],
   strategy="market_data", queries=5, tags=["volatile", "24_7_market"],
   reasons=["Real-time price data available but very volatile"])

_r(r"\b(crypto\s+regulation|sec\s+(vs|sue|lawsuit|approve|etf)|bitcoin\s+etf|spot\s+etf)\b",
   "CRYPTO", "crypto_regulation",
   researchability=75, sources=["SEC.gov", "CoinDesk", "The Block"],
   strategy="news_analysis", queries=6, tags=["regulatory"],
   reasons=["SEC filings and court docs are public"])

_r(r"\b(bitcoin|crypto|ethereum)\b.{0,50}\b(halving|merge|upgrade|fork|launch)\b",
   "CRYPTO", "crypto_events",
   researchability=78, sources=["CoinDesk", "Ethereum.org", "GitHub"],
   strategy="news_analysis", queries=5, tags=["scheduled_event"],
   reasons=["Protocol upgrades have known schedules"])

_r(r"\b(crypto|bitcoin|btc|ethereum|eth|blockchain|defi|nft)\b",
   "CRYPTO", "general",
   researchability=55, sources=["CoinDesk", "CoinGecko"],
   strategy="market_data", queries=4, tags=["volatile"],
   reasons=["Crypto markets data-rich but volatile"])

# SPORTS
_r(r"\b(super\s+bowl|nfl|nba|ncaa|world\s+series|mlb|nhl|stanley\s+cup|world\s+cup|premier\s+league|champions\s+league)\b",
   "SPORTS", "major_leagues",
   researchability=50, sources=["ESPN", "FiveThirtyEight Sports"],
   strategy="sports_odds", queries=3, tags=["odds_available", "unpredictable"],
   reasons=["Sports odds available; our model has no edge over dedicated sportsbooks"])

_r(r"\b(ufc|mma|boxing|fight|bout|knockout)\b",
   "SPORTS", "combat",
   researchability=40, sources=["ESPN", "Sherdog"],
   strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
   reasons=["Combat sports extremely unpredictable"])

_r(r"\b(score|win\s+game|playoff|championship|mvp|draft\s+pick|season\s+record|sport)\b",
   "SPORTS", "general",
   researchability=40, sources=["ESPN"],
   strategy="sports_odds", queries=2, tags=["odds_available", "unpredictable"],
   reasons=["Sports outcomes hard to predict without domain expertise"])

# WEATHER
_r(r"\b(hurricane|tropical\s+storm|typhoon|cyclone|category\s+[1-5])\b",
   "WEATHER", "severe_weather",
   researchability=72, sources=["NOAA", "NHC", "Weather.gov"],
   strategy="official_data", queries=5, tags=["time_sensitive", "nowcast"],
   reasons=["NOAA provides excellent tracking data for active storms"])

_r(r"\b(temperature|heat\s+(wave|record)|cold\s+(snap|record)|snow|rainfall|drought|flood|precipitation)\b",
   "WEATHER", "forecast",
   researchability=56, sources=["NOAA", "Weather.gov", "AccuWeather"],
   strategy="official_data", queries=4, tags=["nowcast"],
   reasons=["Weather forecasts degrade beyond 7-10 days"])

_r(r"\b(earthquake|wildfire|tornado|volcan|tsunami)\b",
   "WEATHER", "natural_disaster",
   researchability=35, sources=["USGS", "NOAA"],
   strategy="official_data", queries=3, tags=["unpredictable"],
   reasons=["Natural disasters are inherently unpredictable"])

# SCIENCE
_r(r"\b(fda|drug\s+approval|clinical\s+trial|phase\s+[123]|pdufa|pharma)\b",
   "SCIENCE", "pharma",
   researchability=82, sources=["FDA.gov", "ClinicalTrials.gov", "STAT News"],
   strategy="official_data", queries=6, tags=["scheduled_event", "regulatory"],
   reasons=["PDUFA dates are scheduled; clinical trial data on ClinicalTrials.gov"])

_r(r"\b(spacex|nasa|rocket|launch|satellite|orbit|mars|moon|artemis|starship)\b",
   "SCIENCE", "space",
   researchability=80, sources=["NASA.gov", "SpaceX", "Space.com"],
   strategy="news_analysis", queries=5, tags=["scheduled_event"],
   reasons=["Launch windows are publicly scheduled"])

# TECH
_r(r"\b(ai\s+(model|regulation|safety|company)|openai|gpt|anthropic|google\s+(ai|gemini)|artificial\s+intelligence|llm)\b",
   "TECH", "ai",
   researchability=62, sources=["TechCrunch", "The Verge", "ArXiv"],
   strategy="news_analysis", queries=5, tags=["fast_moving"],
   reasons=["AI news moves fast -- hard to predict specifics"])

_r(r"\b(apple|google|microsoft|meta|amazon|tesla)\b.{0,40}\b(launch|announc|releas|product|feature)\b",
   "TECH", "product_launch",
   researchability=65, sources=["The Verge", "TechCrunch", "company blogs"],
   strategy="news_analysis", queries=5, tags=["corporate_action"],
   reasons=["Tech launch rumors are common but unreliable"])

# FINANCE
_r(r"\b(earnings|revenue|profit|quarterly\s+results|eps|beat\s+estimate|miss\s+estimate)\b",
   "FINANCE", "earnings",
   researchability=85, sources=["SEC EDGAR", "Yahoo Finance", "Bloomberg"],
   strategy="official_data", queries=7, tags=["scheduled_event", "data_release"],
   reasons=["Earnings dates known in advance; analyst consensus estimates available"])

_r(r"\b(ipo|initial\s+public\s+offering|going\s+public|direct\s+listing|spac)\b",
   "FINANCE", "ipo",
   researchability=72, sources=["SEC EDGAR", "Bloomberg"],
   strategy="news_analysis", queries=5, tags=["corporate_action"],
   reasons=["IPO filings are public (S-1)"])

_r(r"\b(merger|acquisition|acquire|buyout|takeover|m&a|deal\s+close)\b",
   "FINANCE", "mna",
   researchability=78, sources=["SEC EDGAR", "Reuters", "Bloomberg"],
   strategy="news_analysis", queries=6, tags=["corporate_action"],
   reasons=["M&A filings and regulatory approvals are public"])

_r(r"\b(stock|share\s+price|market\s+cap)\b.{0,30}\b(above|below|reach|hit|\$)\b",
   "FINANCE", "stock_price",
   researchability=55, sources=["Yahoo Finance", "Bloomberg"],
   strategy="market_data", queries=4, tags=["volatile", "price_target"],
   reasons=["Stock prices are public but volatile"])

# LEGAL
_r(r"\b(supreme\s+court|scotus|circuit\s+court|federal\s+court|court\s+rul(e|ing))\b",
   "LEGAL", "court_cases",
   researchability=80, sources=["SCOTUS Blog", "court filings", "Reuters"],
   strategy="news_analysis", queries=6, tags=["legal_proceeding"],
   reasons=["Court calendars and oral argument dates are public"])

_r(r"\b(indict(ment|ed)?|convict|guilty|acquit|sentenc|trial\s+verdict)\b",
   "LEGAL", "criminal",
   researchability=75, sources=["PACER", "Reuters", "AP News"],
   strategy="news_analysis", queries=5, tags=["legal_proceeding"],
   reasons=["Trial schedules and filings are public record"])

_r(r"\b(antitrust|ftc|doj\s+(su|investigat)|regulatory\s+(action|fine|probe)|fda\s+(approv|reject))\b",
   "LEGAL", "regulatory",
   researchability=78, sources=["FTC.gov", "FDA.gov", "Reuters"],
   strategy="official_data", queries=6, tags=["regulatory"],
   reasons=["Regulatory filings and decisions are public"])

# CULTURE (low researchability -- mostly skip)
_r(r"\b(celebrity|dating|breakup|engaged|married|baby\s+name|divorce|oscars?|emmy|grammy|golden\s+globe)\b",
   "CULTURE", "entertainment",
   researchability=20, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "low_signal"],
   reasons=["Entertainment events are difficult to research reliably"])

_r(r"\b(tweet|twitter|elon\s+musk\s+(tweet|post|say)|follower\s+count|viral|tiktok)\b",
   "CULTURE", "social_media",
   researchability=12, sources=[],
   strategy="skip", queries=2, tags=["unpredictable", "noise"],
   reasons=["Social media behavior is nearly impossible to predict"])


# ---------------------------------------------------------------------------
# Researchability thresholds
# ---------------------------------------------------------------------------

# Categories that should NEVER be researched
_SKIP_CATEGORIES: frozenset = frozenset({"CULTURE"})

# Default score when no rule matches
_DEFAULT_RESEARCHABILITY = 30


# ---------------------------------------------------------------------------
# Classifier engine
# ---------------------------------------------------------------------------

def classify_kalshi_market(
    ticker: str = "",
    title: str = "",
    description: str = "",
) -> KalshiMarketClassification:
    """Classify a Kalshi market.

    Ticker prefix takes precedence over text-match rules.

    Args:
        ticker: Kalshi market ticker (e.g. "KXFED-25DEC-T4.75").
        title: Market title / question string.
        description: Optional market description for additional context.

    Returns:
        KalshiMarketClassification with full analysis.
    """
    # --- 1. Ticker prefix matching ---
    ticker_upper = ticker.upper() if ticker else ""
    for prefix, cat, sub, cfg in _TICKER_PREFIX_RULES:
        if ticker_upper.startswith(prefix):
            reasons = list(cfg.get("reasons", []))
            sources = list(cfg.get("sources", []))
            strategy = str(cfg.get("strategy", "news_analysis"))
            queries = int(cfg.get("queries", 4))
            researchability = int(cfg.get("researchability", 50))
            tags = list(cfg.get("tags", []))
            worth = researchability >= 25 and cat not in _SKIP_CATEGORIES

            log.info(
                "[research] classifier: ticker=%s prefix=%s -> category=%s sub=%s "
                "researchability=%d",
                ticker, prefix, cat, sub, researchability,
            )
            return KalshiMarketClassification(
                category=cat,
                subcategory=sub,
                researchability_score=researchability,
                researchability_reasons=reasons,
                primary_sources=sources,
                search_strategy=strategy,
                query_budget=queries,
                worth_researching=worth,
                confidence=0.95,  # High confidence -- ticker match is definitive
                tags=tags,
                ticker_matched=True,
            )

    # --- 2. Text-match fallback ---
    text = f"{title} {description}".strip()
    for pattern, cat, sub, cfg in _TEXT_RULES:
        if pattern.search(text):
            reasons = list(cfg.get("reasons", []))
            sources = list(cfg.get("sources", []))
            strategy = str(cfg.get("strategy", "news_analysis"))
            queries = int(cfg.get("queries", 4))
            researchability = int(cfg.get("researchability", 50))
            tags = list(cfg.get("tags", []))
            worth = researchability >= 25 and cat not in _SKIP_CATEGORIES

            # Confidence is higher if pattern matched in title vs only description
            conf = 0.85 if pattern.search(title) else 0.65

            log.info(
                "[research] classifier: ticker=%s text_match -> category=%s sub=%s "
                "researchability=%d",
                ticker, cat, sub, researchability,
            )
            return KalshiMarketClassification(
                category=cat,
                subcategory=sub,
                researchability_score=researchability,
                researchability_reasons=reasons,
                primary_sources=sources,
                search_strategy=strategy,
                query_budget=queries,
                worth_researching=worth,
                confidence=conf,
                tags=tags,
                ticker_matched=False,
            )

    # --- 3. Unknown fallback ---
    log.info(
        "[research] classifier: ticker=%s -> category=OTHER (no rule matched)",
        ticker,
    )
    return KalshiMarketClassification(
        category="OTHER",
        subcategory="unknown",
        researchability_score=_DEFAULT_RESEARCHABILITY,
        researchability_reasons=["No matching classification rule"],
        primary_sources=[],
        search_strategy="news_analysis",
        query_budget=3,
        worth_researching=False,
        confidence=0.2,
        tags=["unclassified"],
        ticker_matched=False,
    )
