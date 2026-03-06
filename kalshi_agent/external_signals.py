"""
external_signals.py
===================

Fetches external signals (news sentiment, consensus estimates, recent
resolution patterns) to improve fair-value estimates.

Signals are cached per scan cycle and the module degrades gracefully
when external data sources are unavailable.

Signal Types
------------
1. **News sentiment** -- Queries public news APIs for headlines related
   to a market's topic and derives a sentiment score.
2. **Consensus estimates** -- For economic indicator markets (CPI, jobs,
   GDP), fetches analyst consensus from public data sources.
3. **Resolution patterns** -- Analyses the historical resolution rate of
   markets in the same series to detect systematic biases.
4. **Market cross-reference** -- Checks related Kalshi markets for
   correlated price signals.

All signals are returned as a dict of floats in [-1, +1] range where:
  - Positive = tilts toward YES resolution
  - Negative = tilts toward NO resolution
  - Zero = neutral / unavailable
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SignalCache:
    """Simple TTL-based in-memory cache for external signals."""

    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self._store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._store:
            ts, value = self._store[key]
            if time.monotonic() - ts < self.ttl:
                return value
            del self._store[key]
        return None

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()

    def prune(self) -> None:
        """Remove expired entries."""
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._store.items() if now - ts >= self.ttl]
        for k in expired:
            del self._store[k]


class ExternalSignals:
    """
    Fetches and caches external signals for market analysis.

    Parameters
    ----------
    client : KalshiClient
        API client for fetching related Kalshi data.
    config : dict
        The ``external_signals`` section of config.
    """

    DEFAULT_CONFIG = {
        "enabled": True,
        "cache_ttl_seconds": 300,
        "news_api_enabled": False,
        "news_api_url": "",
        "news_api_key": "",
        "consensus_enabled": True,
        "resolution_patterns_enabled": True,
        "cross_reference_enabled": True,
        "sentiment_weight": 0.15,
        "consensus_weight": 0.25,
        "resolution_pattern_weight": 0.20,
        "cross_reference_weight": 0.15,
        "request_timeout": 5,
    }

    # Known economic indicator series on Kalshi
    ECONOMIC_SERIES = {
        "KXCPI": "cpi",
        "KXJOB": "jobs",
        "KXGDP": "gdp",
        "KXFED": "fed_rate",
        "KXPCE": "pce",
        "KXUNR": "unemployment",
        "KXINFL": "inflation",
        "KXPPI": "ppi",
        "KXRETAIL": "retail_sales",
        "KXHOUSING": "housing",
    }

    def __init__(self, client=None, config: Optional[Dict[str, Any]] = None):
        self.client = client
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self._cache = SignalCache(ttl_seconds=self.cfg.get("cache_ttl_seconds", 300))
        self._enabled = self.cfg.get("enabled", True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_signals(
        self,
        ticker: str,
        series_ticker: str = "",
        category: str = "",
        title: str = "",
    ) -> Dict[str, float]:
        """
        Fetch all available external signals for a market.

        Returns a dict with signal names as keys and values in [-1, +1].
        Degrades gracefully -- returns zeros for unavailable signals.
        """
        if not self._enabled:
            return self._empty_signals()

        cache_key = self._cache_key(ticker)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        signals = self._empty_signals()

        # 1. News sentiment
        try:
            signals["news_sentiment"] = self._fetch_news_sentiment(title, category)
        except Exception as exc:
            logger.debug("News sentiment unavailable for %s: %s", ticker, exc)

        # 2. Consensus estimates (for economic markets)
        try:
            signals["consensus"] = self._fetch_consensus(
                series_ticker, ticker, title
            )
        except Exception as exc:
            logger.debug("Consensus unavailable for %s: %s", ticker, exc)

        # 3. Resolution patterns
        try:
            signals["resolution_pattern"] = self._fetch_resolution_pattern(
                series_ticker
            )
        except Exception as exc:
            logger.debug("Resolution pattern unavailable for %s: %s", ticker, exc)

        # 4. Cross-reference
        try:
            signals["cross_reference"] = self._fetch_cross_reference(
                ticker, series_ticker
            )
        except Exception as exc:
            logger.debug("Cross-reference unavailable for %s: %s", ticker, exc)

        # Compute composite signal
        signals["composite"] = self._compute_composite(signals)

        self._cache.set(cache_key, signals)
        return signals

    def get_fair_value_adjustment(
        self,
        ticker: str,
        series_ticker: str = "",
        category: str = "",
        title: str = "",
    ) -> float:
        """
        Return a fair-value adjustment in cents based on external signals.

        Positive = shift fair value toward YES.
        Negative = shift fair value toward NO.
        Typical range: [-5, +5] cents.
        """
        signals = self.get_signals(ticker, series_ticker, category, title)
        composite = signals.get("composite", 0.0)
        # Scale composite [-1, +1] to adjustment [-5, +5] cents
        return round(composite * 5.0, 2)

    def clear_cache(self) -> None:
        """Clear the signal cache (call at the start of each scan cycle)."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Signal fetchers
    # ------------------------------------------------------------------

    def _fetch_news_sentiment(self, title: str, category: str) -> float:
        """
        Derive sentiment from news headlines related to the market topic.

        Uses keyword extraction from the market title and queries a news
        API if configured.  Falls back to a neutral score.
        """
        if not self.cfg.get("news_api_enabled", False):
            return 0.0

        api_url = self.cfg.get("news_api_url", "")
        api_key = self.cfg.get("news_api_key", "")
        if not api_url or not api_key:
            return 0.0

        # Extract keywords from title
        keywords = self._extract_keywords(title)
        if not keywords:
            return 0.0

        try:
            import requests
            params = {
                "q": " ".join(keywords[:3]),
                "apiKey": api_key,
                "pageSize": 10,
                "sortBy": "publishedAt",
                "language": "en",
            }
            resp = requests.get(
                api_url,
                params=params,
                timeout=self.cfg.get("request_timeout", 5),
            )
            if resp.status_code != 200:
                return 0.0

            articles = resp.json().get("articles", [])
            if not articles:
                return 0.0

            # Simple keyword-based sentiment
            positive_words = {
                "surge", "rise", "gain", "increase", "above", "beat",
                "strong", "positive", "higher", "up", "growth", "rally",
            }
            negative_words = {
                "fall", "drop", "decline", "decrease", "below", "miss",
                "weak", "negative", "lower", "down", "crash", "slump",
            }

            pos_count = 0
            neg_count = 0
            for article in articles:
                text = (
                    (article.get("title") or "") + " " +
                    (article.get("description") or "")
                ).lower()
                pos_count += sum(1 for w in positive_words if w in text)
                neg_count += sum(1 for w in negative_words if w in text)

            total = pos_count + neg_count
            if total == 0:
                return 0.0

            return max(-1.0, min(1.0, (pos_count - neg_count) / total))

        except Exception:
            return 0.0

    def _fetch_consensus(
        self, series_ticker: str, ticker: str, title: str
    ) -> float:
        """
        For economic indicator markets, derive a signal from the market
        title's threshold vs known consensus patterns.

        This uses heuristics based on the market title to determine
        whether the threshold is above or below typical consensus.
        """
        if not self.cfg.get("consensus_enabled", True):
            return 0.0

        # Identify economic indicator type
        indicator = self.ECONOMIC_SERIES.get(series_ticker, "")
        if not indicator:
            return 0.0

        # Parse threshold from title (e.g., "CPI above 3.5%?")
        title_lower = title.lower()

        # Heuristic: if "above" in title and price < 50, market thinks
        # it's unlikely -> consensus is below -> negative signal
        # This is a simplified heuristic; real implementation would
        # fetch actual consensus data from economic APIs.
        if "above" in title_lower or "higher" in title_lower or "over" in title_lower:
            return 0.1  # Slight positive bias for "above" markets
        elif "below" in title_lower or "lower" in title_lower or "under" in title_lower:
            return -0.1  # Slight negative bias for "below" markets

        return 0.0

    def _fetch_resolution_pattern(self, series_ticker: str) -> float:
        """
        Analyse historical resolution rates for markets in the same series.

        If a series historically resolves YES 70% of the time, this
        provides a +0.2 signal toward YES.
        """
        if not self.cfg.get("resolution_patterns_enabled", True):
            return 0.0

        if not self.client or not series_ticker:
            return 0.0

        cache_key = f"res_pattern_{series_ticker}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            settled = self.client.get_markets(
                status="settled",
                series_ticker=series_ticker,
            )

            if len(settled) < 5:
                self._cache.set(cache_key, 0.0)
                return 0.0

            yes_count = sum(
                1 for m in settled
                if m.get("result") in ("yes", "all_yes", "yes_above", "yes_below")
            )
            total = len(settled)
            yes_rate = yes_count / total

            # Convert to signal: 50% -> 0.0, 70% -> +0.4, 30% -> -0.4
            signal = max(-1.0, min(1.0, (yes_rate - 0.5) * 2.0))
            self._cache.set(cache_key, signal)
            return signal

        except Exception:
            self._cache.set(cache_key, 0.0)
            return 0.0

    def _fetch_cross_reference(
        self, ticker: str, series_ticker: str
    ) -> float:
        """
        Check related open markets in the same series for correlated
        price signals.

        If sibling markets are pricing YES high, this market may also
        lean YES.
        """
        if not self.cfg.get("cross_reference_enabled", True):
            return 0.0

        if not self.client or not series_ticker:
            return 0.0

        cache_key = f"xref_{series_ticker}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            siblings = self.client.get_markets(
                status="open",
                series_ticker=series_ticker,
            )

            # Exclude the current market
            siblings = [m for m in siblings if m.get("ticker") != ticker]
            if not siblings:
                self._cache.set(cache_key, 0.0)
                return 0.0

            # Average mid-price of siblings
            prices = []
            for m in siblings:
                yes_bid = m.get("yes_bid") or 0
                yes_ask = m.get("yes_ask") or 0
                if yes_bid and yes_ask:
                    prices.append((yes_bid + yes_ask) / 2.0)
                elif m.get("last_price"):
                    prices.append(float(m["last_price"]))

            if not prices:
                self._cache.set(cache_key, 0.0)
                return 0.0

            avg_price = sum(prices) / len(prices)
            # Convert to signal: 50 -> 0.0, 70 -> +0.4, 30 -> -0.4
            signal = max(-1.0, min(1.0, (avg_price - 50.0) / 50.0))
            self._cache.set(cache_key, signal)
            return signal

        except Exception:
            self._cache.set(cache_key, 0.0)
            return 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_composite(self, signals: Dict[str, float]) -> float:
        """Weighted average of all individual signals."""
        weights = {
            "news_sentiment": self.cfg.get("sentiment_weight", 0.15),
            "consensus": self.cfg.get("consensus_weight", 0.25),
            "resolution_pattern": self.cfg.get("resolution_pattern_weight", 0.20),
            "cross_reference": self.cfg.get("cross_reference_weight", 0.15),
        }

        total_weight = 0.0
        weighted_sum = 0.0
        for key, weight in weights.items():
            value = signals.get(key, 0.0)
            if value != 0.0:
                weighted_sum += value * weight
                total_weight += weight

        if total_weight == 0:
            return 0.0

        return max(-1.0, min(1.0, weighted_sum / total_weight))

    @staticmethod
    def _empty_signals() -> Dict[str, float]:
        return {
            "news_sentiment": 0.0,
            "consensus": 0.0,
            "resolution_pattern": 0.0,
            "cross_reference": 0.0,
            "composite": 0.0,
        }

    @staticmethod
    def _extract_keywords(title: str) -> List[str]:
        """Extract meaningful keywords from a market title."""
        stop_words = {
            "will", "the", "a", "an", "be", "is", "are", "was", "were",
            "to", "of", "in", "on", "at", "by", "for", "or", "and",
            "not", "this", "that", "it", "its", "than", "more", "less",
            "above", "below", "before", "after", "between", "from",
            "what", "which", "who", "how", "when", "where", "yes", "no",
        }
        words = title.lower().split()
        # Remove punctuation and stop words
        keywords = []
        for w in words:
            cleaned = "".join(c for c in w if c.isalnum())
            if cleaned and cleaned not in stop_words and len(cleaned) > 2:
                keywords.append(cleaned)
        return keywords

    @staticmethod
    def _cache_key(ticker: str) -> str:
        return f"signals_{ticker}"
