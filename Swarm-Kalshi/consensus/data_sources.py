"""
Read-only data sources for WHALE-OS agents.

Every source here is public and needs no credentials:

* Kalshi public market data (markets, orderbook, trades, candlesticks)
* Open-Meteo daily max temperature (several weather models)
* Coinbase Exchange spot price and candles
* Polymarket Gamma market prices

Nothing in this module can place an order.  Each reader takes an optional
``get_json`` callable so tests can inject canned responses.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from consensus.schema import MarketSnapshot

log = logging.getLogger(__name__)

GetJson = Callable[[str, Optional[Dict[str, Any]]], Any]

KALSHI_PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
COINBASE_BASE = "https://api.exchange.coinbase.com"
POLYMARKET_GAMMA_BASE = "https://gamma-api.polymarket.com"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class HttpJson:
    """Small polite JSON getter: per-host spacing, timeout, 429 backoff."""

    def __init__(self, min_interval_s: float = 0.2, timeout_s: float = 15.0,
                 max_retries: int = 3, user_agent: str = "whale-os/1.0 (read-only)") -> None:
        import requests  # local import keeps tests free of network deps

        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.min_interval_s = float(min_interval_s)
        self.timeout_s = float(timeout_s)
        self.max_retries = int(max_retries)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def _space(self, host: str) -> None:
        with self._lock:
            wait = self._last.get(host, 0.0) + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def __call__(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        host = urlparse(url).netloc
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self._space(host)
            resp = self._session.get(url, params=params, timeout=self.timeout_s)
            if resp.status_code == 429 and attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"rate limited: {url}")


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def to_cents(value: Any) -> int:
    """Kalshi dollar strings ("0.4400") or legacy cent ints -> int cents."""
    if value in (None, ""):
        return 0
    if isinstance(value, int):
        return value
    f = float(value)
    return int(round(f * 100)) if f <= 1.0 else int(round(f))


def to_count(value: Any) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def parse_ts(value: Any) -> Optional[float]:
    """ISO-8601 string -> epoch seconds."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _cents_field(m: Dict[str, Any], new: str, old: str) -> int:
    v = m.get(new)
    if v in (None, ""):
        v = m.get(old)
    return to_cents(v)


def _count_field(m: Dict[str, Any], new: str, old: str) -> int:
    v = m.get(new)
    if v in (None, ""):
        v = m.get(old)
    return to_count(v)


def snapshot_from_market(m: Dict[str, Any], now: Optional[float] = None) -> MarketSnapshot:
    """Raw Kalshi market dict -> MarketSnapshot."""
    now = time.time() if now is None else now
    ticker = str(m.get("ticker", ""))
    close_ts = parse_ts(m.get("close_time"))
    hours = (close_ts - now) / 3600.0 if close_ts else None
    rules = "\n".join(str(m.get(k) or "") for k in ("rules_primary", "rules_secondary")).strip()
    return MarketSnapshot(
        ticker=ticker,
        yes_bid=_cents_field(m, "yes_bid_dollars", "yes_bid"),
        yes_ask=_cents_field(m, "yes_ask_dollars", "yes_ask"),
        no_bid=_cents_field(m, "no_bid_dollars", "no_bid"),
        no_ask=_cents_field(m, "no_ask_dollars", "no_ask"),
        series_ticker=str(m.get("series_ticker") or (ticker.split("-")[0] if ticker else "")),
        title=str(m.get("title") or ""),
        category=str(m.get("category") or ""),
        last_price=_cents_field(m, "last_price_dollars", "last_price"),
        volume_24h=_count_field(m, "volume_24h_fp", "volume_24h"),
        open_interest=_count_field(m, "open_interest_fp", "open_interest"),
        liquidity_cents=_cents_field(m, "liquidity_dollars", "liquidity"),
        hours_to_close=hours,
        rules_text=rules,
    )


def parse_orderbook(data: Dict[str, Any]) -> Dict[str, List[List[float]]]:
    """Kalshi orderbook -> {"yes": [[cents, qty], ...], "no": [...]} (bids per side)."""
    if "orderbook_fp" in data:
        fp = data["orderbook_fp"] or {}
        raw = {"yes": fp.get("yes_dollars") or [], "no": fp.get("no_dollars") or []}
    else:
        ob = data.get("orderbook", data) or {}
        raw = {"yes": ob.get("yes") or [], "no": ob.get("no") or []}
    out: Dict[str, List[List[float]]] = {"yes": [], "no": []}
    for side, levels in raw.items():
        for lvl in levels:
            try:
                out[side].append([to_cents(lvl[0]), float(lvl[1])])
            except (TypeError, ValueError, IndexError):
                continue
    return out


def parse_trade(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    side = str(t.get("taker_side") or "").lower()
    price = t.get("yes_price_dollars")
    if price in (None, ""):
        price = t.get("yes_price")
    ts = parse_ts(t.get("created_time"))
    count = to_count(t.get("count_fp") if t.get("count_fp") not in (None, "") else t.get("count"))
    if side not in ("yes", "no") or price in (None, "") or ts is None or count <= 0:
        return None
    return {"taker_side": side, "yes_price": to_cents(price), "count": count, "ts": ts}


# --------------------------------------------------------------------------- #
# Kalshi public
# --------------------------------------------------------------------------- #

class KalshiPublic:
    def __init__(self, get_json: Optional[GetJson] = None, base_url: str = KALSHI_PUBLIC_BASE) -> None:
        self._get = get_json or HttpJson(min_interval_s=0.15)
        self.base = base_url.rstrip("/")

    def get_markets(self, series_ticker: str, status: str = "open",
                    limit: int = 200, max_pages: int = 5) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(max_pages):
            params: Dict[str, Any] = {"series_ticker": series_ticker, "status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"{self.base}/markets", params) or {}
            out.extend(data.get("markets") or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> Dict[str, Any]:
        data = self._get(f"{self.base}/markets/{ticker}", None) or {}
        return data.get("market", data)

    def get_orderbook(self, ticker: str) -> Dict[str, List[List[float]]]:
        return parse_orderbook(self._get(f"{self.base}/markets/{ticker}/orderbook", None) or {})

    def get_trades(self, ticker: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = self._get(f"{self.base}/markets/trades", {"ticker": ticker, "limit": limit}) or {}
        return [p for p in (parse_trade(t) for t in data.get("trades") or []) if p]

    def get_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 60) -> List[Dict[str, Any]]:
        data = self._get(
            f"{self.base}/series/{series_ticker}/markets/{ticker}/candlesticks",
            {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_interval},
        ) or {}
        return data.get("candlesticks") or []


# --------------------------------------------------------------------------- #
# Weather / crypto / cross-venue
# --------------------------------------------------------------------------- #

class OpenMeteo:
    def __init__(self, get_json: Optional[GetJson] = None, base_url: str = OPEN_METEO_BASE) -> None:
        self._get = get_json or HttpJson(min_interval_s=0.5)
        self.base = base_url

    def daily_max_f(self, lat: float, lon: float, tz: str, date_iso: str,
                    models: List[str]) -> Dict[str, float]:
        """Forecast daily max (degF) for ``date_iso`` from each model that returns one."""
        params = {
            "latitude": lat, "longitude": lon, "timezone": tz,
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "start_date": date_iso, "end_date": date_iso,
        }
        if models:
            params["models"] = ",".join(models)
        data = self._get(self.base, params) or {}
        if data.get("error"):
            raise RuntimeError(f"open-meteo: {data.get('reason')}")
        daily = data.get("daily") or {}
        times = daily.get("time") or []
        if date_iso not in times:
            return {}
        i = times.index(date_iso)
        out: Dict[str, float] = {}
        for key, series in daily.items():
            if not key.startswith("temperature_2m_max"):
                continue
            model = key[len("temperature_2m_max"):].lstrip("_") or (models[0] if len(models) == 1 else "default")
            try:
                val = series[i]
            except (IndexError, TypeError):
                continue
            if val is not None:
                out[model] = float(val)
        return out


class Coinbase:
    def __init__(self, get_json: Optional[GetJson] = None, base_url: str = COINBASE_BASE) -> None:
        self._get = get_json or HttpJson(min_interval_s=0.2)
        self.base = base_url.rstrip("/")

    def spot(self, product: str) -> float:
        data = self._get(f"{self.base}/products/{product}/ticker", None) or {}
        return float(data["price"])

    def closes(self, product: str, granularity: int = 300) -> List[float]:
        """Chronological candle closes (Coinbase returns newest first)."""
        rows = self._get(f"{self.base}/products/{product}/candles", {"granularity": granularity}) or []
        rows = sorted((r for r in rows if isinstance(r, list) and len(r) >= 5), key=lambda r: r[0])
        return [float(r[4]) for r in rows]


class Polymarket:
    def __init__(self, get_json: Optional[GetJson] = None, base_url: str = POLYMARKET_GAMMA_BASE) -> None:
        self._get = get_json or HttpJson(min_interval_s=0.3)
        self.base = base_url.rstrip("/")

    def outcome_price(self, slug: str, outcome: str = "Yes") -> Optional[float]:
        rows = self._get(f"{self.base}/markets", {"slug": slug}) or []
        if isinstance(rows, dict):
            rows = rows.get("markets") or [rows]
        for m in rows:
            outcomes = m.get("outcomes")
            prices = m.get("outcomePrices")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not outcomes or not prices:
                continue
            for name, price in zip(outcomes, prices):
                if str(name).lower() == outcome.lower():
                    return float(price)
        return None
