"""
Deterministic rules veto (the "rugcheck" lane).

No LLM is involved.  Any blocked rule vetoes the market regardless of how
strongly the agents agree.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Dict, List, Mapping, Optional, Tuple

from consensus.schema import MarketSnapshot, VetoResult


@dataclass(frozen=True)
class VetoConfig:
    require_two_sided: bool = True
    max_spread_cents: int = 6
    min_open_interest: int = 0
    min_liquidity_cents: int = 0
    min_volume_24h: int = 0
    min_hours_to_close: Optional[float] = 0.25
    max_hours_to_close: Optional[float] = 24.0 * 30
    min_mid_cents: float = 3.0
    max_mid_cents: float = 97.0
    # Multi-leg combo markets dominated the March losses; blocked by default.
    blocked_series_prefixes: Tuple[str, ...] = ("KXMVE",)
    # Phrases in resolution rules that signal ambiguous settlement.
    blocked_rule_terms: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "VetoConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs: Dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("blocked_series_prefixes", "blocked_rule_terms"):
                value = tuple(str(v) for v in (value or ()))
            kwargs[key] = value
        return cls(**kwargs)


class VetoRules:
    def __init__(self, config: Optional[VetoConfig] = None) -> None:
        self.config = config or VetoConfig()

    def evaluate(self, market: MarketSnapshot) -> List[VetoResult]:
        c = self.config
        out: List[VetoResult] = []

        if c.require_two_sided:
            ok = market.two_sided
            out.append(VetoResult("two_sided_book", not ok,
                                  "" if ok else f"bid={market.yes_bid} ask={market.yes_ask}"))

        spread = market.yes_spread_cents
        out.append(VetoResult("max_spread", spread > c.max_spread_cents,
                              f"spread={spread}c limit={c.max_spread_cents}c"
                              if spread > c.max_spread_cents else ""))

        mid = market.mid_cents
        in_band = c.min_mid_cents <= mid <= c.max_mid_cents
        out.append(VetoResult("price_band", not in_band,
                              "" if in_band else
                              f"mid={mid:.1f}c outside [{c.min_mid_cents}, {c.max_mid_cents}]"))

        for name, value, floor in (
            ("min_open_interest", market.open_interest, c.min_open_interest),
            ("min_liquidity", market.liquidity_cents, c.min_liquidity_cents),
            ("min_volume_24h", market.volume_24h, c.min_volume_24h),
        ):
            blocked = floor > 0 and value < floor
            out.append(VetoResult(name, blocked, f"{value} < {floor}" if blocked else ""))

        hours = market.hours_to_close
        if hours is not None:
            too_soon = c.min_hours_to_close is not None and hours < c.min_hours_to_close
            too_far = c.max_hours_to_close is not None and hours > c.max_hours_to_close
            reason = ""
            if too_soon:
                reason = f"closes in {hours:.2f}h < {c.min_hours_to_close}h"
            elif too_far:
                reason = f"closes in {hours:.1f}h > {c.max_hours_to_close}h"
            out.append(VetoResult("time_to_close", too_soon or too_far, reason))

        series = (market.series_ticker or market.ticker).upper()
        hit = next((p for p in c.blocked_series_prefixes if series.startswith(p.upper())), None)
        out.append(VetoResult("blocked_series", hit is not None,
                              f"series {series} matches {hit}" if hit else ""))

        if c.blocked_rule_terms and market.rules_text:
            text = market.rules_text.lower()
            term = next((t for t in c.blocked_rule_terms if t.lower() in text), None)
            out.append(VetoResult("ambiguous_rules", term is not None,
                                  f"rules contain {term!r}" if term else ""))

        return out
