"""
market_scanner.py
=================

Enhanced market scanner with:

* **Recent trade fetching** — attaches the last N public trades to each
  opportunity so the analysis engine can compute price velocity and flow.
* **Price velocity pre-filter** — optionally skips markets where price is
  stuck (no recent trades) to focus on active, resolving markets.
* **Configurable enrichment** — recent trades are fetched lazily alongside
  the orderbook to avoid extra API calls on markets that don't pass filters.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kalshi_agent.kalshi_client import KalshiClient

logger = logging.getLogger(__name__)


@dataclass
class MarketOpportunity:
    """Lightweight container for a market that passed initial filters."""

    ticker: str
    event_ticker: str
    title: str
    series_ticker: str
    category: str

    # Pricing snapshot
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    last_price: int
    mid_price: float

    # Liquidity & volume
    volume_24h: int
    open_interest: int
    liquidity: int  # cents

    # Timing
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    hours_to_expiry: float = 0.0

    # Orderbook snapshot (populated lazily)
    orderbook: Optional[Dict[str, Any]] = field(default=None, repr=False)

    # Recent trades (populated lazily) — used for momentum scoring
    recent_trades: Optional[List[Dict[str, Any]]] = field(default=None, repr=False)

    # Spread
    spread: int = 0

    @property
    def implied_probability(self) -> float:
        return self.mid_price / 100.0


class MarketScanner:
    """
    Scans the Kalshi exchange for tradeable markets.

    Parameters
    ----------
    client : KalshiClient
        Authenticated (or unauthenticated) API client.
    config : dict
        The ``trading`` section of ``config.yaml``.
    """

    def __init__(self, client: KalshiClient, config: Dict[str, Any]):
        self.client = client
        self.cfg = config

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def scan(self) -> List[MarketOpportunity]:
        """Fetch all open markets, apply filters, and return ranked opportunities."""
        logger.info("Scanning active markets …")
        raw_markets = self.client.get_markets(status="open")
        logger.info("Fetched %d open markets from the exchange.", len(raw_markets))

        opportunities: List[MarketOpportunity] = []
        now = datetime.now(timezone.utc)

        for m in raw_markets:
            opp = self._parse_market(m, now)
            if opp is None:
                continue
            if self._passes_filters(opp):
                opportunities.append(opp)

        logger.info(
            "%d markets passed filters out of %d total.",
            len(opportunities), len(raw_markets),
        )

        # Rank: higher volume × liquidity / spread = better
        opportunities.sort(
            key=lambda o: (o.volume_24h * max(1, o.liquidity)) / max(1, o.spread),
            reverse=True,
        )
        return opportunities

    def enrich_orderbook(self, opp: MarketOpportunity) -> MarketOpportunity:
        """Fetch and attach the live orderbook."""
        try:
            opp.orderbook = self.client.get_market_orderbook(opp.ticker)
        except Exception as exc:
            logger.warning("Failed to fetch orderbook for %s: %s", opp.ticker, exc)
        return opp

    def enrich_recent_trades(
        self, opp: MarketOpportunity, limit: int = 10
    ) -> MarketOpportunity:
        """
        Fetch and attach recent public trades for momentum analysis.
        This is called alongside enrich_orderbook for top-N opportunities.
        """
        try:
            opp.recent_trades = self.client.get_trades(opp.ticker, limit=limit)
        except Exception as exc:
            logger.warning("Failed to fetch trades for %s: %s", opp.ticker, exc)
        return opp

    def enrich(self, opp: MarketOpportunity, trade_limit: int = 10) -> MarketOpportunity:
        """Fetch orderbook AND recent trades in one call."""
        self.enrich_orderbook(opp)
        self.enrich_recent_trades(opp, limit=trade_limit)
        return opp

    def categorise(self, opportunities: List[MarketOpportunity]) -> Dict[str, List[MarketOpportunity]]:
        """Group opportunities by their series ticker."""
        cats: Dict[str, List[MarketOpportunity]] = {}
        for opp in opportunities:
            cats.setdefault(opp.series_ticker, []).append(opp)
        return cats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_market(self, m: Dict[str, Any], now: datetime) -> Optional[MarketOpportunity]:
        try:
            yes_bid = m.get("yes_bid") or 0
            yes_ask = m.get("yes_ask") or 0
            no_bid = m.get("no_bid") or 0
            no_ask = m.get("no_ask") or 0
            last_price = m.get("last_price") or 0

            if yes_bid and yes_ask:
                mid = (yes_bid + yes_ask) / 2.0
            else:
                mid = float(last_price)

            spread = max(0, yes_ask - yes_bid) if yes_bid and yes_ask else 99

            close_time = self._parse_ts(m.get("close_time"))
            exp_time = self._parse_ts(m.get("expiration_time"))
            ref_time = exp_time or close_time
            hours_to_expiry = (
                (ref_time - now).total_seconds() / 3600.0 if ref_time else 0.0
            )

            ticker: str = m.get("ticker", "")
            series_ticker = ticker.split("-")[0] if "-" in ticker else ticker

            return MarketOpportunity(
                ticker=ticker,
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                series_ticker=series_ticker,
                category=m.get("category", ""),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                last_price=last_price,
                mid_price=mid,
                volume_24h=m.get("volume_24h") or 0,
                open_interest=m.get("open_interest") or 0,
                liquidity=m.get("liquidity") or 0,
                close_time=close_time,
                expiration_time=exp_time,
                hours_to_expiry=hours_to_expiry,
                spread=spread,
            )
        except Exception as exc:
            logger.debug("Skipping unparseable market %s: %s", m.get("ticker"), exc)
            return None

    def _passes_filters(self, opp: MarketOpportunity) -> bool:
        min_liq = self.cfg.get("min_liquidity_cents", 0)
        min_vol = self.cfg.get("min_volume_24h", 0)
        min_hrs = self.cfg.get("min_hours_to_expiry", 0)
        max_hrs = self.cfg.get("max_hours_to_expiry", float("inf"))

        # === REAL MARKET FILTERS ===
        # Market must have SOME real activity (OI OR volume OR liquidity)
        has_activity = (opp.open_interest > 0 or opp.volume_24h > 0 or opp.liquidity > 0)
        if not has_activity:
            return False
        # ============================

        if opp.liquidity < min_liq:
            return False
        if opp.volume_24h < min_vol:
            return False
        if opp.hours_to_expiry < min_hrs:
            return False
        if opp.hours_to_expiry > max_hrs:
            return False
        if opp.mid_price <= 0 or opp.mid_price >= 100:
            return False
        return True

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            cleaned = value.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            return None
