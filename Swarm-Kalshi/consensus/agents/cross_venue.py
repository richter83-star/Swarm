"""
XVENUE agent: the same event's price on Polymarket.

Matching Kalshi and Polymarket markets automatically is unreliable (wording,
strikes and resolution sources differ), so this agent only votes on markets
you map explicitly in config:

    cross_venue:
      enabled: true
      mappings:
        - kalshi: "KXFEDDECISION-26DEC-H0"      # exact ticker, or
          kalshi_regex: "^KXFED.*-H0$"          # a regex
          polymarket_slug: "fed-decision-in-december"
          outcome: "Yes"
          invert: false                         # true if YES on one is NO on the other

Read-only: uses Polymarket's public Gamma API.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.data_sources import Polymarket


class CrossVenueAgent(Agent):
    name = "xvenue"
    family = "cross_venue"
    label = "XVENUE"

    def __init__(self, config=None, source: Optional[Polymarket] = None) -> None:
        super().__init__(config)
        self.source = source or Polymarket()
        self.mappings: List[Dict[str, Any]] = list(self.config.get("mappings") or [])

    def _mapping(self, ticker: str) -> Optional[Dict[str, Any]]:
        for m in self.mappings:
            if m.get("kalshi") and m["kalshi"] == ticker:
                return m
            if m.get("kalshi_regex") and re.search(m["kalshi_regex"], ticker):
                return m
        return None

    def applies(self, ctx: MarketContext) -> bool:
        return self._mapping(ctx.ticker) is not None

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        m = self._mapping(ctx.ticker)
        if not m:
            return None
        price = self.source.outcome_price(m["polymarket_slug"], m.get("outcome", "Yes"))
        if price is None or not 0.0 < price < 1.0:
            return None
        p = 1.0 - price if m.get("invert") else price
        return Estimate(
            p_yes=p,
            confidence=float(m.get("confidence", 0.6)),
            rationale=f"polymarket {m['polymarket_slug']} {m.get('outcome', 'Yes')} = {price:.3f}"
                      + (" (inverted)" if m.get("invert") else ""),
            sources=(f"polymarket:{m['polymarket_slug']}",),
        )
