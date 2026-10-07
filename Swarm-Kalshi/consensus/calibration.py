"""
Build per-series price calibration tables from settled Kalshi markets.

For each settled market in a series, take the YES mid price ``lead_hours``
before close (hourly candlesticks) and record whether it resolved YES.
Bucketing by price gives the empirical YES rate at each price level - the
favourite/longshot bias for that series.  The HISTORY agent reads the table.

CLI (public data, no credentials):

    python -m consensus.calibration --series KXHIGHNY,KXHIGHCHI \
        --leads 6,24 --max-markets 300 --out data/whale_os/calibration.json

    python -m consensus.calibration      # every series in config/whale_os.yaml,
                                         # written where the HISTORY agent reads it
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
    # one-sided books are real prices too: 0c bid / 1c ask is a ~0.5c market
    if 0 <= bid < ask <= 100 and (bid > 0 or ask < 100):
        return min(99, max(1, int(round((bid + ask) / 2))))
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
    if lead_hours < 2:      # sub-hour leads (hourly crypto markets): 1-minute candles
        candles = reader.get_candlesticks(series, market["ticker"], int(target - 15 * 60),
                                          int(target + 60), period_interval=1)
    else:
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


def _strike(m: Dict[str, Any]) -> float:
    for k in ("floor_strike", "cap_strike"):
        try:
            if m.get(k) is not None:
                return float(m[k])
        except (TypeError, ValueError):
            pass
    return 0.0


def _atm_index(ladder: List[Dict[str, Any]], price_at) -> int:
    """Index of the strike priced nearest 50c, by binary search (prices are monotone in
    strike along a ladder).  Falls back to the middle when prices are unavailable."""
    lo, hi = 0, len(ladder) - 1
    seen = {lo: price_at(ladder[lo]), hi: price_at(ladder[hi])}
    if seen[lo] is None or seen[hi] is None or seen[lo] == seen[hi]:
        return len(ladder) // 2
    falling = seen[lo] > seen[hi]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        seen[mid] = price_at(ladder[mid])
        if seen[mid] is None:
            return mid
        if (seen[mid] > 50) == falling:
            lo = mid
        else:
            hi = mid
    return lo if abs(seen[lo] - 50) <= abs(seen[hi] - 50) else hi


def sample_by_event(markets: List[Dict[str, Any]], per_event: int, max_events: int,
                    price_at=None) -> List[Dict[str, Any]]:
    """
    Up to ``per_event`` strikes from each of the newest ``max_events`` events, centred on
    the strike that was priced nearest 50c (``price_at``, evaluated at the EARLIEST lead,
    so later leads never see selection based on their own future).  Selecting on price
    is safe for a calibration table: the table is P(yes | price), and price is exactly
    what is conditioned on.  Without ``price_at`` the ladder middle is used.
    """
    events: Dict[str, List[Dict[str, Any]]] = {}
    for m in markets:
        events.setdefault(str(m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]), []).append(m)
    newest = sorted(events.values(), key=lambda ms: max(parse_ts(x.get("close_time")) or 0 for x in ms),
                    reverse=True)[:max_events]
    out: List[Dict[str, Any]] = []
    for ms in newest:
        ladder = sorted(ms, key=_strike)
        mid = _atm_index(ladder, price_at) if price_at and len(ladder) > 2 else len(ladder) // 2
        lo = max(0, min(mid - per_event // 2, len(ladder) - per_event))
        out.extend(ladder[lo:lo + per_event])
    return out


def build_series(reader: KalshiPublic, series: str, leads: List[float],
                 max_markets: int = 300, bin_width: int = 10,
                 per_event: int = 0, max_events: int = 0) -> Dict[str, Any]:
    if per_event and max_events:
        # many strikes per event (hourly crypto): page through enough events, then sample
        pages = max(1, min(400, (max_events * 120 + 199) // 200))
        settled = [m for m in reader.get_markets(series, status="settled", limit=200, max_pages=pages)
                   if m.get("result") in ("yes", "no")]
        earliest = max(leads)

        def price_at(m: Dict[str, Any]) -> Optional[int]:
            try:
                return market_price_at(reader, series, m, earliest)
            except Exception:
                return None
        markets = sample_by_event(settled, per_event, max_events, price_at)
    else:
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
    events = len({str(m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]) for m in markets})
    return {"built_at": time.time(), "markets": len(markets), "events": events, "leads": by_lead}


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


DEFAULT_PLAN = {"leads": [12.0, 24.0, 36.0], "max_markets": 300, "per_event": 0, "max_events": 0,
                "bin_width": 10}


def plan_for(series: str, cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build plan for one series: defaults <- calibration.* <- calibration.per_series[series]."""
    cfg = dict(cfg or {})
    per = (cfg.pop("per_series", None) or {}).get(series) or {}
    plan = {**DEFAULT_PLAN, **{k: v for k, v in cfg.items() if k in DEFAULT_PLAN},
            **{k: v for k, v in per.items() if k in DEFAULT_PLAN}}
    plan["leads"] = [float(x) for x in plan["leads"]]
    return plan


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Build WHALE-OS price calibration tables")
    ap.add_argument("--series", default="", help="comma-separated series tickers "
                    "(default: every series in whale_os.yaml)")
    ap.add_argument("--config", default=None, help="path to whale_os.yaml")
    ap.add_argument("--leads", default="", help="hours before close, comma-separated "
                    "(default: whale_os.yaml calibration plan per series)")
    ap.add_argument("--max-markets", type=int, default=0)
    ap.add_argument("--out", default="", help="default: the HISTORY agent's table_path")
    args = ap.parse_args(argv)
    from consensus.settings import load_settings
    settings = load_settings(args.config)
    series_list = [s.strip().upper() for s in (args.series or ",".join(settings.series)).split(",") if s.strip()]
    out = args.out or str((settings.agents.get("history") or {}).get("table_path") or settings.calibration_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    reader = KalshiPublic()
    table = load_table(out)
    for series in series_list:
        plan = plan_for(series, settings.calibration)
        if args.leads:
            plan["leads"] = [float(x) for x in args.leads.split(",") if x.strip()]
        if args.max_markets:
            plan["max_markets"] = args.max_markets
        log.info("%s plan: %s", series, plan)
        table.setdefault("series", {})[series] = build_series(
            reader, series, plan["leads"], int(plan["max_markets"]), int(plan["bin_width"]),
            int(plan["per_event"]), int(plan["max_events"]))
        save_table(out, table)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
