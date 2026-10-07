"""
WHALE-OS HUD: a read-only Flask app over the shadow ledger.

    python -m consensus.hud                       # http://127.0.0.1:8890
    python -m consensus.hud --port 8891 --config config/whale_os.yaml
"""

from __future__ import annotations

import argparse
import os
import re
import threading
import time
from typing import Any, Dict, Optional

from consensus.data_sources import KalshiPublic, to_cents
from consensus.ledger import DecisionLedger
from consensus.reliability import ReliabilityStore
from consensus.settings import WhaleOSSettings, load_settings
from consensus.hud.state import build_state

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{1,80}$")


def _candles(raw) -> list:
    out = []
    for c in raw or []:
        price = c.get("price") or {}
        bid = to_cents((c.get("yes_bid") or {}).get("close_dollars"))
        ask = to_cents((c.get("yes_ask") or {}).get("close_dollars"))
        mid = (bid + ask) / 2.0 if 0 < bid < ask < 100 else None
        close = to_cents(price.get("close_dollars")) or None
        o = to_cents(price.get("open_dollars")) or close or mid
        h = to_cents(price.get("high_dollars")) or close or mid
        lo = to_cents(price.get("low_dollars")) or close or mid
        c_ = close or mid
        if c_ is None:
            continue
        out.append({"t": c.get("end_period_ts"), "o": o, "h": h, "l": lo, "c": c_,
                    "v": float(c.get("volume_fp") or c.get("volume") or 0)})
    return out


def market_snapshot(reader: Any, ticker: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Live order book + 24h hourly candles for one market (public data)."""
    now = time.time() if now is None else now
    m = reader.get_market(ticker)
    series = str(m.get("series_ticker") or ticker.split("-")[0])
    book = reader.get_orderbook(ticker)
    candles = _candles(reader.get_candlesticks(series, ticker, int(now - 36 * 3600), int(now), 60))
    return {"ticker": ticker, "title": m.get("title", ""), "book": book, "candles": candles,
            "yes_bid": to_cents(m.get("yes_bid_dollars") or m.get("yes_bid")),
            "yes_ask": to_cents(m.get("yes_ask_dollars") or m.get("yes_ask")),
            "volume_24h": float(m.get("volume_24h_fp") or m.get("volume_24h") or 0)}


def create_app(settings: WhaleOSSettings, ledger: Optional[DecisionLedger] = None,
               reliability: Optional[ReliabilityStore] = None, reader: Any = None):
    from flask import Flask, jsonify, send_from_directory

    app = Flask(__name__, static_folder=None)
    ledger = ledger or DecisionLedger(settings.ledger_path, mode=settings.mode)
    reliability = reliability or ReliabilityStore(settings.reliability_path)
    refresh = int((settings.hud or {}).get("refresh_seconds", 10))
    market_ttl = float((settings.hud or {}).get("market_cache_s", 15))
    reader_box: Dict[str, Any] = {"reader": reader}
    cache: Dict[str, Any] = {}
    lock = threading.Lock()

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/state")
    def state():
        data = build_state(settings, ledger, reliability)
        data["refresh_seconds"] = refresh
        return jsonify(data)

    @app.get("/api/market/<ticker>")
    def market(ticker: str):
        ticker = ticker.upper()
        if not _TICKER_RE.match(ticker):
            return jsonify({"error": "invalid ticker"}), 400
        with lock:
            hit = cache.get(ticker)
            if hit and time.time() - hit[0] < market_ttl:
                return jsonify(hit[1])
        try:
            if reader_box["reader"] is None:
                reader_box["reader"] = KalshiPublic()
            data = market_snapshot(reader_box["reader"], ticker)
        except Exception as exc:
            data = {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"[:200]}
        with lock:
            cache[ticker] = (time.time(), data)
        return jsonify(data)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mode": settings.mode}

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WHALE-OS HUD (read-only)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)
    settings = load_settings(args.config)
    hud = settings.hud or {}
    app = create_app(settings)
    app.run(host=args.host or hud.get("host", "127.0.0.1"),
            port=args.port or int(hud.get("port", 8890)), debug=False, threaded=True)
    return 0
