"""
WHALES agent: recent taker flow on Kalshi.

Signed taker volume over a window (YES-taker contracts minus NO-taker
contracts) nudges fair value away from mid, capped at ``max_shift_cents``.
Large single prints ("whales") are flagged in the rationale.  Same data
family as the order book, so the engine counts BOOK + WHALES as one vote.
"""

from __future__ import annotations

from typing import Optional

from consensus.agents.base import Agent, Estimate, MarketContext


class FlowAgent(Agent):
    name = "whales"
    family = "microstructure"
    label = "WHALES"

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        window_s = float(self.config.get("window_minutes", 60)) * 60.0
        min_trades = int(self.config.get("min_trades", 5))
        max_shift = float(self.config.get("max_shift_cents", 4.0))
        size_norm = float(self.config.get("size_norm_contracts", 500.0))
        whale_size = int(self.config.get("whale_contracts", 250))

        recent = [t for t in ctx.trades() if ctx.now - t["ts"] <= window_s]
        if len(recent) < min_trades:
            return None
        total = sum(t["count"] for t in recent)
        if total <= 0:
            return None
        signed = sum(t["count"] if t["taker_side"] == "yes" else -t["count"] for t in recent)
        imbalance = signed / total
        intensity = min(1.0, total / size_norm)
        mid = ctx.snapshot.mid_cents
        p_cents = min(max(mid + imbalance * max_shift * intensity, 1.0), 99.0)
        whales = [t for t in recent if t["count"] >= whale_size]
        whale_note = ""
        if whales:
            ys = sum(1 for t in whales if t["taker_side"] == "yes")
            whale_note = f", {len(whales)} whale print(s) ({ys} yes / {len(whales) - ys} no)"
        return Estimate(
            p_yes=p_cents / 100.0,
            confidence=0.3 + 0.5 * intensity,
            rationale=f"{len(recent)} trades / {total} contracts, imbalance {imbalance:+.2f}{whale_note}",
            sources=("kalshi:trades",),
        )
