"""
Build per-series price calibration tables from settled Kalshi markets.

For each settled market in a series, take the YES mid price ``lead_hours``
before close (hourly candlesticks) and record whether it resolved YES.
Bucketing by price gives the empirical YES rate at each price level - the
favourite/longshot bias for that series.  The HISTORY agent reads the table.

CLI (public data, no credentials):

    python -m consensus.calibration --series KXHIGHNY,KXHIGHCHI \
        --leads 6,24 --max-markets 300 --out data/whale_os/calibration.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from consensus.data_sources import KalshiPublic, parse_ts, to_cents

log = logging.getLogger(__name__)


def _candle_mid_cents(c: Dict[str, Any]) -> Optional[int]:
    bid = to_cents((c.get("yes_bid") or {}).get("close_dollars"))
    ask = to_cents((c.get("yes_ask") or {}).get("close_dollars"))
    if 0 < bid < ask < 100:
        return int(round((bid + ask) / 2))
    price = c.get("price") or {}
    p = to_cents(price.get("close_dollars") or price.get("previous_dollars"))
    return p if 0 < p < 100 else None


def market_price_at(reader: KalshiPublic, series: str, market: Dict[str, Any],
                    lead_hours: float) -> Optional[int]:
    close = parse_ts(market.get("close_time"))
    opened = parse_ts(market.get("open_time"))
    if close is None:
        return None
    target = close - lead_hours * 3600.0
    if opened is not None and target < opened:
        return None
    candles = reader.get_candlesticks(series, market["ticker"], int(target - 3 * 3600),
                                      int(target + 3600), period_interval=60)
    best: Optional[Tuple[float, int]] = None
    for c in candles:
        end = c.get("end_period_ts")
        mid = _candle_mid_cents(c)
        if end is None or mid is None or end > target + 60:
            continue
        gap = target - float(end)
        if best is None or gap < best[0]:
            best = (gap, mid)
    return best[1] if best else None


def bucket(rows: List[Tuple[int, bool]], bin_width: int = 10) -> List[Dict[str, Any]]:
    bins = []
    for lo in range(0, 100, bin_width):
        hi = lo + bin_width
        sel = [(p, y) for p, y in rows if lo <= p < hi or (hi == 100 and p == 99)]
        if not sel:
            continue
        bins.append({
            "lo": lo, "hi": hi, "n": len(sel),
            "yes": sum(1 for _, y in sel if y),
            "avg_price": round(sum(p for p, _ in sel) / len(sel), 3),
        })
    return bins


def build_series(reader: KalshiPublic, series: str, leads: List[float],
                 max_markets: int = 300, bin_width: int = 10) -> Dict[str, Any]:
    pages = max(1, (max_markets + 199) // 200)
    markets = [m for m in reader.get_markets(series, status="settled", limit=200, max_pages=pages)
               if m.get("result") in ("yes", "no")][:max_markets]
    by_lead: Dict[str, Any] = {}
    for lead in leads:
        rows: List[Tuple[int, bool]] = []
        for m in markets:
            try:
                p = market_price_at(reader, series, m, lead)
            except Exception as exc:
                log.debug("candles failed %s: %s", m.get("ticker"), exc)
                continue
            if p is not None:
                rows.append((p, m["result"] == "yes"))
        by_lead[str(lead)] = {"n": len(rows), "bins": bucket(rows, bin_width)}
        log.info("%s lead %sh: %d samples", series, lead, len(rows))
    return {"built_at": time.time(), "markets": len(markets), "leads": by_lead}


def load_table(path: str) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {"version": 1, "series": {}}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_table(path: str, table: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(table, fh, indent=2)
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build WHALE-OS price calibration tables")
    ap.add_argument("--series", required=True, help="comma-separated series tickers")
    ap.add_argument("--leads", default="6,24", help="hours before close, comma-separated")
    ap.add_argument("--max-markets", type=int, default=300)
    ap.add_argument("--bin-width", type=int, default=10)
    ap.add_argument("--out", default="data/whale_os/calibration.json")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    reader = KalshiPublic()
    table = load_table(args.out)
    leads = [float(x) for x in args.leads.split(",") if x.strip()]
    for series in [s.strip().upper() for s in args.series.split(",") if s.strip()]:
        table.setdefault("series", {})[series] = build_series(
            reader, series, leads, args.max_markets, args.bin_width)
        save_table(args.out, table)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
