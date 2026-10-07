"""
MODEL agent (crypto): price-level markets (KXBTC*, KXETH*, ...).

Zero-drift lognormal from Coinbase spot with volatility estimated from
recent 5-minute candles, inflated for fat tails.  Kalshi settles crypto
markets on a CF Benchmarks index average near close; Coinbase spot is a
close proxy, not the settlement source.  Short-dated crypto markets are
the most bot-crowded on Kalshi - expect this agent to rarely disagree with
the market, and let the reliability store prove otherwise.
"""

from __future__ import annotations

import math
import statistics
from typing import Dict, Optional

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.agents.strikes import prob_lognormal, yes_interval
from consensus.data_sources import Coinbase

DEFAULT_PRODUCTS = {
    "KXBTC": "BTC-USD",
    "KXETH": "ETH-USD",
    "KXSOL": "SOL-USD",
    "KXXRP": "XRP-USD",
    "KXDOGE": "DOGE-USD",
}


class CryptoAgent(Agent):
    name = "crypto"
    family = "crypto_model"
    label = "MODEL"

    def __init__(self, config=None, source: Optional[Coinbase] = None) -> None:
        super().__init__(config)
        self.source = source or Coinbase()
        self.products: Dict[str, str] = {**DEFAULT_PRODUCTS, **(self.config.get("products") or {})}
        self.granularity = int(self.config.get("granularity_s", 300))
        self.tail_factor = float(self.config.get("tail_factor", 1.2))
        self.min_seconds = float(self.config.get("min_seconds_to_close", 180))
        self.max_hours = float(self.config.get("max_hours_to_close", 48))
        self.vol_cache_ttl_s = float(self.config.get("vol_cache_ttl_s", 300))
        self._vol_cache: Dict[str, tuple] = {}

    def _product(self, series: str) -> Optional[str]:
        best = None
        for prefix, product in self.products.items():
            if series.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
                best = (prefix, product)
        return best[1] if best else None

    def applies(self, ctx: MarketContext) -> bool:
        secs = ctx.seconds_to_close
        return (self._product(ctx.series) is not None and bool(ctx.strike_type)
                and secs is not None and self.min_seconds <= secs <= self.max_hours * 3600)

    def _sigma_per_sqrt_s(self, product: str, now: float) -> float:
        hit = self._vol_cache.get(product)
        if hit and now - hit[0] < self.vol_cache_ttl_s:
            return hit[1]
        closes = self.source.closes(product, self.granularity)
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        if len(rets) < 20:
            raise RuntimeError(f"not enough candles for {product}: {len(rets)}")
        sig = statistics.stdev(rets) / math.sqrt(self.granularity) * self.tail_factor
        self._vol_cache[product] = (now, sig)
        return sig

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        product = self._product(ctx.series)
        interval = yes_interval(ctx.strike_type, ctx.floor_strike, ctx.cap_strike)
        if product is None or interval is None:
            return None
        spot = self.source.spot(product)
        tau = float(ctx.seconds_to_close)
        sig = self._sigma_per_sqrt_s(product, ctx.now)
        total = sig * math.sqrt(tau)
        p = prob_lognormal(interval, spot, total)
        ann = sig * math.sqrt(365 * 24 * 3600)
        return Estimate(
            p_yes=p,
            confidence=0.5,
            rationale=f"{product} spot {spot:,.2f}, {tau / 3600:.2f}h left, vol {ann:.0%} ann. "
                      f"(x{self.tail_factor} tails)",
            sources=(f"coinbase:{product}",),
        )
