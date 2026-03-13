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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    created_time: Optional[datetime] = None
    updated_time: Optional[datetime] = None
    hours_to_expiry: float = 0.0
    last_trade_time: Optional[datetime] = None
    recent_trade_count: int = 0

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
        self._market_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def scan(self) -> List[MarketOpportunity]:
        """Fetch open markets, apply filters, and return ranked opportunities."""
        scan_mode = str(self.cfg.get("scan_mode", "recent_focus")).lower()
        if scan_mode == "full_universe":
            return self._scan_full_universe()
        return self._scan_recent_focus()

    def _scan_full_universe(self) -> List[MarketOpportunity]:
        """Legacy scanner: walk open-market pages then filter locally."""
        logger.info("Scanning active markets (mode=full_universe) …")
        max_pages = int(self.cfg.get("market_scan_max_pages", 50))
        markets_result = self.client.get_markets(
            status="open",
            max_pages=max_pages,
            return_meta=True,
        )
        raw_markets, page_meta = markets_result
        scanned_count = len(raw_markets)
        truncated = bool((page_meta or {}).get("truncated", False))
        pages_fetched = int((page_meta or {}).get("pages_fetched", 0))

        if truncated:
            logger.warning(
                "Fetched %d open markets from the exchange (scanned subset; "
                "pagination cap hit at %d pages).",
                scanned_count, pages_fetched,
            )
        else:
            logger.info(
                "Fetched %d open markets from the exchange across %d pages.",
                scanned_count, pages_fetched,
            )

        opportunities: List[MarketOpportunity] = []
        now = datetime.now(timezone.utc)

        for m in raw_markets:
            opp = self._parse_market(m, now)
            if opp is None:
                continue
            if self._passes_filters(opp):
                opportunities.append(opp)

        if truncated:
            logger.info(
                "%d markets passed filters out of %d scanned (subset; total open universe is larger).",
                len(opportunities), scanned_count,
            )
        else:
            logger.info(
                "%d markets passed filters out of %d scanned open markets.",
                len(opportunities), scanned_count,
            )

        # Rank: higher volume × liquidity / spread = better
        opportunities.sort(
            key=lambda o: (o.volume_24h * max(1, o.liquidity)) / max(1, o.spread),
            reverse=True,
        )
        return opportunities

    def _scan_recent_focus(self) -> List[MarketOpportunity]:
        """
        Recent-focus scanner:
        1) Pull recent global trades to identify active tickers.
        2) Fetch current market snapshot only for those tickers.
        3) Apply base filters + focus filters for liquidity/recency.
        """
        logger.info("Scanning active markets (mode=recent_focus) …")
        now = datetime.now(timezone.utc)

        recent_pages = int(self.cfg.get("recent_trade_seed_pages", 3))
        recent_limit = int(self.cfg.get("recent_trade_seed_page_size", 200))
        top_tickers = int(self.cfg.get("recent_trade_seed_top_tickers", 80))
        cache_ttl = float(self.cfg.get("market_cache_ttl_seconds", 180))

        seed_map, trades_fetched = self._build_recent_trade_seed(
            page_size=recent_limit,
            max_pages=recent_pages,
        )
        if not seed_map:
            logger.warning(
                "Recent-focus seed returned no trades; falling back to full_universe scan."
            )
            return self._scan_full_universe()

        ranked_tickers = sorted(
            seed_map.items(),
            key=lambda kv: (
                kv[1].get("trade_count", 0),
                kv[1].get("last_trade_time") or datetime.fromtimestamp(0, timezone.utc),
            ),
            reverse=True,
        )
        candidate_tickers = [t for t, _ in ranked_tickers[:top_tickers]]
        logger.info(
            "Recent-focus seed fetched %d trades across %d tickers; evaluating top %d.",
            trades_fetched, len(seed_map), len(candidate_tickers),
        )

        opportunities: List[MarketOpportunity] = []
        for ticker in candidate_tickers:
            market = self._get_market_cached(ticker, ttl_seconds=cache_ttl)
            if not market:
                continue

            opp = self._parse_market(market, now)
            if opp is None:
                continue

            seed = seed_map.get(ticker, {})
            opp.recent_trade_count = int(seed.get("trade_count", 0) or 0)
            opp.last_trade_time = seed.get("last_trade_time")

            if self._passes_filters(opp) and self._passes_focus_filters(opp, now):
                opportunities.append(opp)

        logger.info(
            "Recent-focus: %d markets passed filters out of %d seeded tickers.",
            len(opportunities), len(candidate_tickers),
        )

        opportunities.sort(
            key=lambda o: ((o.recent_trade_count + 1) * (o.volume_24h + 1) * max(1, o.liquidity)) / max(1, o.spread),
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
            opp.recent_trades = self.client.get_trades(
                ticker=opp.ticker,
                limit=limit,
                max_pages=1,
            )
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
            # API returns prices as dollar strings with _dollars suffix; convert to cents.
            def _cents(key_new: str, key_old: str) -> int:
                v = m.get(key_new) or m.get(key_old) or 0
                return int(round(float(v) * 100))

            def _count(key_new: str, key_old: str) -> int:
                v = m.get(key_new) or m.get(key_old) or 0
                return int(float(v))

            yes_bid = _cents("yes_bid_dollars", "yes_bid")
            yes_ask = _cents("yes_ask_dollars", "yes_ask")
            no_bid = _cents("no_bid_dollars", "no_bid")
            no_ask = _cents("no_ask_dollars", "no_ask")
            last_price = _cents("last_price_dollars", "last_price")

            if yes_bid and yes_ask:
                mid = (yes_bid + yes_ask) / 2.0
            else:
                mid = float(last_price)

            spread = max(0, yes_ask - yes_bid) if yes_bid and yes_ask else 99

            close_time = self._parse_ts(m.get("close_time"))
            exp_time = self._parse_ts(m.get("expiration_time"))
            created_time = self._parse_ts(m.get("created_time"))
            updated_time = self._parse_ts(m.get("updated_time"))
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
                volume_24h=_count("volume_24h_fp", "volume_24h"),
                open_interest=_count("open_interest_fp", "open_interest"),
                liquidity=_cents("liquidity_dollars", "liquidity"),
                close_time=close_time,
                expiration_time=exp_time,
                created_time=created_time,
                updated_time=updated_time,
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

        # --- Excluded series prefixes (parlay / cross-category contracts) ---
        excluded_series: list = self.cfg.get(
            "excluded_series_prefixes", ["KXMVECROSS"]
        )
        for prefix in excluded_series:
            if opp.series_ticker.upper().startswith(prefix.upper()):
                return False

        # === REAL MARKET FILTERS ===
        # Market must have SOME real activity (OI OR volume OR liquidity)
        has_activity = (opp.open_interest > 0 or opp.volume_24h > 0 or opp.liquidity > 0)
        if not has_activity:
            return False
        # ============================

        # Liquidity field is often 0 for active markets; accept if either
        # liquidity OR 24h volume meets threshold.
        if opp.liquidity < min_liq and opp.volume_24h < min_vol:
            return False
        if opp.hours_to_expiry < min_hrs:
            return False
        if opp.hours_to_expiry > max_hrs:
            return False
        if opp.mid_price <= 0 or opp.mid_price >= 100:
            return False
        return True

    def _passes_focus_filters(self, opp: MarketOpportunity, now: datetime) -> bool:
        """
        Additional recent-focus constraints to prioritize high-liquidity
        and recently active markets.
        """
        base_min_liq = int(self.cfg.get("min_liquidity_cents", 0))
        base_min_vol = int(self.cfg.get("min_volume_24h", 0))

        min_focus_liq = int(self.cfg.get("focus_min_liquidity_cents", max(base_min_liq, 1000)))
        min_focus_vol = int(self.cfg.get("focus_min_volume_24h", max(base_min_vol, 100)))
        min_focus_oi = int(self.cfg.get("focus_min_open_interest", 25))
        min_recent_trades = int(self.cfg.get("focus_min_recent_trades", 1))
        recent_hours = float(self.cfg.get("recent_activity_max_age_hours", 24))
        focus_max_hrs = float(
            self.cfg.get("focus_max_hours_to_expiry", self.cfg.get("max_hours_to_expiry", float("inf")))
        )

        high_liquidity = (
            opp.liquidity >= min_focus_liq
            or opp.volume_24h >= min_focus_vol
            or opp.open_interest >= min_focus_oi
        )
        if not high_liquidity:
            return False

        if opp.recent_trade_count < min_recent_trades:
            return False

        ref_ts = opp.last_trade_time or opp.updated_time or opp.created_time
        if ref_ts is None:
            return False
        age_hours = (now - ref_ts).total_seconds() / 3600.0
        if age_hours > recent_hours:
            return False

        if opp.hours_to_expiry > focus_max_hrs:
            return False

        return True

    def _build_recent_trade_seed(
        self,
        page_size: int,
        max_pages: int,
    ) -> Tuple[Dict[str, Dict[str, Any]], int]:
        """
        Return a ticker -> seed metrics map from recent global trades.
        """
        try:
            trades = self.client.get_trades(
                ticker=None,
                limit=page_size,
                max_pages=max_pages,
            )
        except Exception as exc:
            logger.warning("Recent-focus seed fetch failed: %s", exc)
            return {}, 0

        seed: Dict[str, Dict[str, Any]] = {}
        for tr in trades:
            ticker = str(tr.get("ticker", "")).strip()
            if not ticker:
                continue

            count = tr.get("count")
            if count is None:
                try:
                    count = int(float(tr.get("count_fp", 0)))
                except (TypeError, ValueError):
                    count = 0
            try:
                count = int(count)
            except (TypeError, ValueError):
                count = 0

            created_ts = self._parse_ts(tr.get("created_time"))
            cur = seed.setdefault(ticker, {"trade_count": 0, "last_trade_time": None})
            cur["trade_count"] = int(cur["trade_count"]) + max(1, count)
            if created_ts and (
                cur["last_trade_time"] is None or created_ts > cur["last_trade_time"]
            ):
                cur["last_trade_time"] = created_ts

        return seed, len(trades)

    def _get_market_cached(self, ticker: str, ttl_seconds: float) -> Optional[Dict[str, Any]]:
        """Read market snapshot from local TTL cache or fetch from API."""
        now = time.monotonic()
        cached = self._market_cache.get(ticker)
        if cached is not None:
            cached_ts, market = cached
            if (now - cached_ts) <= ttl_seconds:
                return market

        try:
            market = self.client.get_market(ticker)
        except Exception as exc:
            logger.debug("Failed to fetch market snapshot for %s: %s", ticker, exc)
            return None

        if not isinstance(market, dict):
            return None
        status = str(market.get("status", "")).lower()
        if status not in ("open", "active"):
            return None

        self._market_cache[ticker] = (now, market)

        # Prune stale cache entries opportunistically.
        stale = [t for t, (ts, _) in self._market_cache.items() if (now - ts) > (ttl_seconds * 4)]
        for t in stale:
            self._market_cache.pop(t, None)

        return market

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            cleaned = value.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            return None
