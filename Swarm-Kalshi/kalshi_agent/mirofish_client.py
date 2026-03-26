"""
mirofish_client.py
==================

Lightweight client for the MiroFish swarm-intelligence research API.

Fetches a sentiment signal for a Kalshi market question and returns a
normalised float in [-1, +1] suitable for use as a fair-value tilt in
AnalysisEngine._fair_value().

Degrades gracefully — always returns None on any failure so the trading
loop is never stalled by an unavailable research service.

Config keys (under ``mirofish:`` in swarm_config.yaml):
    enabled             bool   Whether to use MiroFish (default: false)
    api_url             str    Base URL  (default: https://mirofish.dracanus.app)
    api_key             str    X-API-Key header value
    cache_ttl_seconds   int    How long to reuse a cached signal (default: 300)
    timeout_seconds     int    HTTP request timeout (default: 8)
    min_confidence      int    Ignore results below this confidence 0-100 (default: 45)
    weight              float  How much to blend MiroFish into ext_tilt (default: 0.40)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class MiroFishClient:
    """
    Fetches and caches MiroFish research signals.

    Parameters
    ----------
    config : dict
        The ``mirofish`` section of swarm_config.yaml.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "enabled":           False,
        "api_url":           "https://mirofish.dracanus.app",
        "api_key":           "",
        "cache_ttl_seconds": 300,
        "timeout_seconds":   8,
        "min_confidence":    45,
        "weight":            0.40,
    }

    # Category mapping: Kalshi category strings → MiroFish categories
    _CATEGORY_MAP: Dict[str, str] = {
        "political":  "politics",
        "politics":   "politics",
        "economic":   "economics",
        "economics":  "economics",
        "financial":  "economics",
        "finance":    "economics",
        "weather":    "weather",
        "climate":    "weather",
        "sports":     "sports",
        "sport":      "sports",
        "science":    "science",
        "technology": "science",
        "crypto":     "crypto",
        "bitcoin":    "crypto",
        "general":    "general",
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.cfg: Dict[str, Any] = {**self.DEFAULT_CONFIG, **(config or {})}
        self._enabled: bool = bool(self.cfg.get("enabled", False))
        self._cache: Dict[str, Tuple[float, Optional[Dict]]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_signal(self, title: str, category: str = "general") -> Optional[float]:
        """
        Return a sentiment tilt in [-1.0, +1.0] for the given market.

        Positive  = evidence tilts toward YES resolution.
        Negative  = evidence tilts toward NO resolution.
        None      = unavailable (disabled, timeout, low confidence, error).

        Results are cached per (title, category) for cache_ttl_seconds.
        """
        if not self._enabled:
            return None

        cache_key = f"{title}|{category}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            logger.debug("[mirofish] cache hit for: %s", title[:60])
            return self._to_tilt(cached)

        data = self._fetch(title, category)
        if data is None:
            return None

        self._set_cache(cache_key, data)
        tilt = self._to_tilt(data)
        logger.info(
            "[mirofish] signal=%.2f conviction=%s conf=%d quality=%.2f live=%s | %s",
            tilt,
            data.get("conviction", "?"),
            data.get("confidence", 0),
            data.get("research_quality", 0),
            data.get("has_live_data", False),
            title[:60],
        )
        return tilt

    @property
    def weight(self) -> float:
        """Blend weight for this signal in fair-value calculation."""
        return float(self.cfg.get("weight", 0.40))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch(self, title: str, category: str) -> Optional[Dict]:
        """Call MiroFish API and return the data dict, or None on failure."""
        api_url  = self.cfg.get("api_url", self.DEFAULT_CONFIG["api_url"]).rstrip("/")
        api_key  = self.cfg.get("api_key", "")
        timeout  = float(self.cfg.get("timeout_seconds", 8))
        min_conf = int(self.cfg.get("min_confidence", 45))

        mf_category = self._CATEGORY_MAP.get(category.lower(), "general")

        try:
            resp = requests.post(
                f"{api_url}/api/research/analyze",
                json={"question": title, "category": mf_category},
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.debug("[mirofish] HTTP %d for: %s", resp.status_code, title[:60])
                return None

            body = resp.json()
            if not body.get("success"):
                logger.debug("[mirofish] API error: %s", body.get("error"))
                return None

            data = body.get("data", {})

            if data.get("confidence", 0) < min_conf:
                logger.debug(
                    "[mirofish] low confidence (%d < %d) — ignoring: %s",
                    data.get("confidence", 0), min_conf, title[:60],
                )
                return None

            return data

        except requests.Timeout:
            logger.debug("[mirofish] timeout after %.1fs for: %s", timeout, title[:60])
            return None
        except Exception as exc:
            logger.debug("[mirofish] fetch failed: %s", exc)
            return None

    @staticmethod
    def _to_tilt(data: Dict) -> float:
        """Convert MiroFish sentiment_score (0–1) to tilt (-1 to +1)."""
        sentiment = float(data.get("sentiment_score", 0.5))
        # Clamp to valid range then scale: 0.5 → 0.0, 1.0 → +1.0, 0.0 → -1.0
        sentiment = max(0.0, min(1.0, sentiment))
        return round((sentiment - 0.5) * 2.0, 4)

    def _get_cache(self, key: str) -> Optional[Dict]:
        """Return cached data if not expired."""
        if key in self._cache:
            ts, data = self._cache[key]
            ttl = float(self.cfg.get("cache_ttl_seconds", 300))
            if time.monotonic() - ts < ttl:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Dict) -> None:
        self._cache[key] = (time.monotonic(), data)

    def clear_cache(self) -> None:
        """Clear all cached signals (call at scan cycle start if desired)."""
        self._cache.clear()
