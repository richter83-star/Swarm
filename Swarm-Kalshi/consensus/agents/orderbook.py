"""
BOOK agent: fair value from Kalshi order-book depth (microprice).

Kalshi's book lists bids on each side.  A NO bid at q cents is a YES ask at
100 - q.  The microprice leans the mid toward the side with *less* resting
size, the usual short-horizon fair-value estimate.  It can never move more
than half the spread from mid, so on tight books it mostly echoes the market
(and falls inside the engine's deadband) - by design.
"""

from __future__ import annotations

import math
from typing import Optional

from consensus.agents.base import Agent, Estimate, MarketContext


class OrderbookAgent(Agent):
    name = "book"
    family = "microstructure"
    label = "BOOK"

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        depth_cents = int(self.config.get("depth_cents", 3))
        ob = ctx.orderbook()
        yes_bids = [lvl for lvl in ob.get("yes", []) if 0 < lvl[0] < 100 and lvl[1] > 0]
        no_bids = [lvl for lvl in ob.get("no", []) if 0 < lvl[0] < 100 and lvl[1] > 0]
        if not yes_bids or not no_bids:
            return None
        best_bid = max(p for p, _ in yes_bids)
        best_no = max(p for p, _ in no_bids)
        best_ask = 100 - best_no
        if best_ask <= best_bid:
            return None
        bid_qty = sum(q for p, q in yes_bids if p >= best_bid - depth_cents)
        ask_qty = sum(q for p, q in no_bids if p >= best_no - depth_cents)
        total = bid_qty + ask_qty
        if total <= 0:
            return None
        micro = (best_bid * ask_qty + best_ask * bid_qty) / total
        imbalance = (bid_qty - ask_qty) / total
        conf = min(1.0, math.log10(1.0 + total) / 4.0)
        return Estimate(
            p_yes=micro / 100.0,
            confidence=conf,
            rationale=f"bid {best_bid}c x{bid_qty:.0f} / ask {best_ask}c x{ask_qty:.0f}, "
                      f"imbalance {imbalance:+.2f}, micro {micro:.2f}c",
            sources=("kalshi:orderbook",),
        )
