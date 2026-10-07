"""
HISTORY agent: series-level price calibration (favourite / longshot bias).

Looks up how often markets in this series that traded near the current
price actually resolved YES (table built by ``consensus.calibration``),
shrinks that rate toward the price when the bucket is thin, and applies the
bias to the current mid.  Abstains when the series has no table or the
bucket has too few samples.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.calibration import load_table


class HistoryAgent(Agent):
    name = "history"
    family = "history"
    label = "HISTORY"

    def __init__(self, config=None, table: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(config)
        self.table = table if table is not None else load_table(self.config.get("table_path", ""))
        self.min_n = int(self.config.get("min_bucket_n", 15))
        self.prior_n = float(self.config.get("prior_n", 20))

    def applies(self, ctx: MarketContext) -> bool:
        return ctx.series in (self.table.get("series") or {})

    def _lead_entry(self, entry: Dict[str, Any], hours: Optional[float]) -> Optional[Dict[str, Any]]:
        leads = entry.get("leads") or {}
        if not leads:
            return None
        if hours is None:
            return next(iter(leads.values()))
        key = min(leads, key=lambda k: abs(float(k) - hours))
        return leads[key]

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        entry = self._lead_entry(self.table["series"][ctx.series], ctx.snapshot.hours_to_close)
        if not entry:
            return None
        mid = ctx.snapshot.mid_cents
        bucket = next((b for b in entry.get("bins", []) if b["lo"] <= mid < b["hi"]), None)
        if bucket is None or bucket["n"] < self.min_n:
            return None
        n, yes, avg = bucket["n"], bucket["yes"], float(bucket["avg_price"])
        rate = (yes + self.prior_n * avg / 100.0) / (n + self.prior_n)
        bias_cents = rate * 100.0 - avg
        p_cents = min(max(mid + bias_cents, 1.0), 99.0)
        return Estimate(
            p_yes=p_cents / 100.0,
            confidence=min(1.0, n / 200.0),
            rationale=f"{bucket['lo']}-{bucket['hi']}c bucket: {yes}/{n} YES vs avg price "
                      f"{avg:.1f}c -> bias {bias_cents:+.1f}c",
            sources=("kalshi:settled-history",),
        )
