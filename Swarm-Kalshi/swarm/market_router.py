"""
market_router.py
================

Routes markets to the correct specialist bot based on category, series
ticker, and title keyword matching.

The router uses a priority system:
1. Series ticker match (highest priority -- e.g., KXCPI -> Oracle)
2. Category field match
3. Title keyword match (fallback)
4. Default to Vanguard (catch-all)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set

import yaml

logger = logging.getLogger(__name__)


class MarketRouter:
    """
    Routes markets to specialist bots based on category matching.

    Parameters
    ----------
    bot_configs : dict
        Mapping of bot_name -> loaded config dict.
    default_bot : str
        Bot name to use when no specialist matches.
    """

    def __init__(
        self,
        bot_configs: Dict[str, Dict[str, Any]],
        default_bot: str = "vanguard",
    ):
        self.default_bot = default_bot
        self._category_map: Dict[str, str] = {}
        self._series_map: Dict[str, str] = {}
        self._keyword_map: Dict[str, List[str]] = {}
        self._excluded_map: Dict[str, Set[str]] = {}

        self._build_routing_tables(bot_configs)

    def _build_routing_tables(self, bot_configs: Dict[str, Dict]) -> None:
        """Build lookup tables from bot configurations."""
        for bot_name, cfg in bot_configs.items():
            # Category filters
            categories = cfg.get("category_filters", [])
            for cat in categories:
                cat_lower = cat.lower().strip()
                if cat_lower not in self._category_map:
                    self._category_map[cat_lower] = bot_name

            # Series filters
            series = cfg.get("series_filters", [])
            for s in series:
                self._series_map[s.upper()] = bot_name

            # Keywords
            keywords = cfg.get("category_keywords", [])
            self._keyword_map[bot_name] = [k.lower() for k in keywords]

            # Excluded categories
            excluded = cfg.get("excluded_categories", [])
            self._excluded_map[bot_name] = {e.lower() for e in excluded}

        logger.info(
            "Market router initialized: %d category rules, %d series rules, %d bots with keywords",
            len(self._category_map),
            len(self._series_map),
            len(self._keyword_map),
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def route(self, market: Dict[str, Any]) -> str:
        """
        Determine which bot should handle a given market.

        Parameters
        ----------
        market : dict
            Raw market dict from the Kalshi API.

        Returns
        -------
        str
            Bot name (e.g., "sentinel", "oracle", "pulse", "vanguard").
        """
        ticker = market.get("ticker", "")
        category = (market.get("category") or "").lower().strip()
        title = (market.get("title") or "").lower()
        series_ticker = ticker.split("-")[0] if "-" in ticker else ticker

        # 1. Series ticker match (highest priority)
        if series_ticker.upper() in self._series_map:
            bot = self._series_map[series_ticker.upper()]
            logger.debug("Routed %s to %s (series match)", ticker, bot)
            return bot

        # 2. Category field match
        if category in self._category_map:
            bot = self._category_map[category]
            logger.debug("Routed %s to %s (category match: %s)", ticker, bot, category)
            return bot

        # 3. Partial category match
        for cat_key, bot_name in self._category_map.items():
            if cat_key in category or category in cat_key:
                logger.debug("Routed %s to %s (partial category: %s)", ticker, bot_name, category)
                return bot_name

        # 4. Title keyword match
        best_bot = None
        best_score = 0
        for bot_name, keywords in self._keyword_map.items():
            score = sum(1 for kw in keywords if kw in title)
            if score > best_score:
                best_score = score
                best_bot = bot_name

        if best_bot and best_score > 0:
            logger.debug(
                "Routed %s to %s (keyword match, score=%d)",
                ticker, best_bot, best_score,
            )
            return best_bot

        # 5. Default
        logger.debug("Routed %s to %s (default)", ticker, self.default_bot)
        return self.default_bot

    def route_batch(self, markets: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
        """
        Route a batch of markets, returning a dict of bot_name -> [markets].
        """
        result: Dict[str, List[Dict]] = {}
        for market in markets:
            bot = self.route(market)
            result.setdefault(bot, []).append(market)

        for bot, mkt_list in result.items():
            logger.info("Routed %d markets to %s", len(mkt_list), bot)

        return result

    def get_routing_stats(self) -> Dict[str, int]:
        """Return counts of routing rules per bot."""
        stats: Dict[str, int] = {}
        for bot in set(self._category_map.values()):
            cats = sum(1 for v in self._category_map.values() if v == bot)
            series = sum(1 for v in self._series_map.values() if v == bot)
            keywords = len(self._keyword_map.get(bot, []))
            stats[bot] = cats + series + keywords
        return stats
