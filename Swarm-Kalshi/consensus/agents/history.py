"""
HISTORY agent: series-level price calibration (favourite / longshot bias).

Looks up how often markets in this series that traded near the current
price actually resolved YES (table built by ``consensus.calibration``),
shrinks that rate toward the price when the bucket is thin, and applies the
bias to the current mid.  Abstains when the series has no table or the
bucket has too few samples.

The table file is re-read when it changes on disk (checked at most every
``reload_check_s`` seconds), so a weekly rebuild takes effect without a
restart of the shadow runner.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.calibration import load_table


class HistoryAgent(Agent):
    name = "history"
    family = "history"
    label = "HISTORY"

    def __init__(self, config=None, table: Optional[Dict[str, Any]] = None,
                 clock=time.monotonic) -> None:
        super().__init__(config)
        self.path = "" if table is not None else str(self.config.get("table_path", "") or "")
        self.reload_check_s = float(self.config.get("reload_check_s", 60))
        self._clock = clock
        self._mtime: Optional[float] = None
        self._checked = clock()
        self.table = table if table is not None else self._read()
        self.min_n = int(self.config.get("min_bucket_n", 15))
        self.prior_n = float(self.config.get("prior_n", 20))
        # a lead "matches" a market within max(lead_tolerance_frac * lead, lead_tolerance_min_h)
        self.lead_frac = float(self.config.get("lead_tolerance_frac", 0.5))
        self.lead_min_h = float(self.config.get("lead_tolerance_min_h", 0.1))

    def _read(self) -> Dict[str, Any]:
        try:
            self._mtime = os.path.getmtime(self.path) if self.path else None
        except OSError:
            self._mtime = None
        try:
            return load_table(self.path)
        except (OSError, ValueError):          # half-written or corrupt: keep running, abstain
            return {"version": 1, "series": {}}

    def maybe_reload(self) -> bool:
        """Re-read the table if the file changed since the last read."""
        if not self.path or self._clock() - self._checked < self.reload_check_s:
            return False
        self._checked = self._clock()
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self.table = self._read()
        return True

    def applies(self, ctx: MarketContext) -> bool:
        self.maybe_reload()
        return ctx.series in (self.table.get("series") or {})

    def _lead_entry(self, entry: Dict[str, Any], hours: Optional[float]) -> Optional[Dict[str, Any]]:
        leads = entry.get("leads") or {}
        if not leads:
            return None
        if hours is None:
            return next(iter(leads.values()))
        key = min(leads, key=lambda k: abs(float(k) - hours))
        # long-shot bias changes as close approaches: a 24h table says little at 30 minutes
        if abs(float(key) - hours) > max(self.lead_frac * float(key), self.lead_min_h):
            return None
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
