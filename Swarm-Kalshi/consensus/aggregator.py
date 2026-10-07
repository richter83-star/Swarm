"""
ConsensusEngine ("Jev"): turns agent votes into a fire / hold / veto decision.

Gates, in order (all are logged on the Decision for the HUD):

1. Rules veto     – any deterministic rule blocks the market.
2. Voter floor    – at least ``min_voters`` non-abstaining, fresh votes.
3. Quorum         – effective agreement on one side >= ``quorum``.  Agreement
                    is counted per *family*: N correlated agents that read the
                    same data count as at most 1 independent vote.
4. Net edge       – pooled probability of the chosen side, minus entry price,
                    minus Kalshi fee per contract >= ``min_edge_cents``.

Pooling is a weighted average in log-odds space.  The market's own mid
price joins the pool as an anchor voter (``market_weight``), so the
engine only departs from the market when the agents collectively insist.
Weights = reliability weight (learned Brier skill vs the market)
          x confidence factor
          / family_size ** correlation_alpha
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from consensus.fees import MAKER_RATE, TAKER_RATE, fee_per_contract_cents
from consensus.reliability import ReliabilityStore
from consensus.schema import AgentVote, Decision, MarketSnapshot, blocked_vetoes
from consensus.vetoes import VetoConfig, VetoRules


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1.0 - 1e-4)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class ConsensusConfig:
    quorum: float = 4.0                 # effective independent agreeing votes
    min_voters: int = 4                 # non-abstaining fresh votes required
    min_edge_cents: float = 2.0         # net of fee, per contract
    direction_deadband_cents: float = 1.0
    market_weight: float = 1.0          # anchor weight of the market mid
    correlation_alpha: float = 1.0      # 1.0 => a family's total weight == one agent
    confidence_floor: float = 0.5       # weight factor = floor + (1-floor)*confidence
    max_vote_age_s: float = 15 * 60
    prefer_maker: bool = False         # shadow evidence is taker-priced; maker fills are not guaranteed
    taker_rate: float = TAKER_RATE
    maker_rate: float = MAKER_RATE
    reference_contracts: int = 1        # order size assumed for fee rounding (P&L is per contract)
    vetoes: VetoConfig = field(default_factory=VetoConfig)

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "ConsensusConfig":
        if not data:
            return cls()
        known = {f.name for f in fields(cls)}
        kwargs: Dict[str, Any] = {k: v for k, v in data.items() if k in known and k != "vetoes"}
        kwargs["vetoes"] = VetoConfig.from_dict(data.get("vetoes"))
        return cls(**kwargs)


class ConsensusEngine:
    def __init__(
        self,
        config: Optional[ConsensusConfig] = None,
        reliability: Optional[ReliabilityStore] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config or ConsensusConfig()
        self.reliability = reliability
        self.vetoes = VetoRules(self.config.vetoes)
        self._clock = clock

    # ------------------------------------------------------------------ #

    def decide(self, market: MarketSnapshot, votes: Iterable[AgentVote]) -> Decision:
        cfg = self.config
        now = self._clock()
        reasons: List[str] = []

        veto_results = self.vetoes.evaluate(market)
        blocked = blocked_vetoes(veto_results)

        latest = self._latest_votes(market.ticker, votes)
        fresh = [v for v in latest if now - v.created_at <= cfg.max_vote_age_s]
        stale = len(latest) - len(fresh)
        if stale:
            reasons.append(f"ignored {stale} stale vote(s)")
        active = [v for v in fresh if not v.abstained]
        abstain_count = len(fresh) - len(active)

        mid = market.mid_cents
        weights = self._weights(active)
        p_pooled = self._pool(active, weights, mid / 100.0)

        side, agree_count, effective_agree = self._majority(active, mid)

        price: Optional[int] = None
        order_type: Optional[str] = None
        fee_pc = 0.0
        edge = 0.0
        if side is not None:
            quote = self._entry_quote(market, side)
            if quote is not None:
                price, order_type, rate = quote
                fee_pc = fee_per_contract_cents(cfg.reference_contracts, price, rate)
                p_side = p_pooled if side == "yes" else 1.0 - p_pooled
                edge = p_side * 100.0 - price - fee_pc

        # ---- gates ----
        if blocked:
            action = "veto"
            reasons.extend(f"veto:{v.name} {v.reason}".strip() for v in blocked)
        elif len(active) < cfg.min_voters:
            action = "hold"
            reasons.append(f"voters {len(active)} < min {cfg.min_voters}")
        elif side is None:
            action = "hold"
            reasons.append("no majority direction")
        elif effective_agree + 1e-9 < cfg.quorum:
            action = "hold"
            reasons.append(f"quorum {effective_agree:.2f} < {cfg.quorum} on {side}")
        elif price is None:
            action = "hold"
            reasons.append(f"no executable quote on {side}")
        elif edge + 1e-9 < cfg.min_edge_cents:
            action = "hold"
            reasons.append(f"net edge {edge:.2f}c < {cfg.min_edge_cents}c after fee {fee_pc:.2f}c")
        else:
            action = "fire"
            reasons.append(
                f"{side} x{agree_count} (eff {effective_agree:.2f}) "
                f"p={p_pooled:.3f} price={price}c fee={fee_pc:.2f}c edge={edge:.2f}c"
            )

        return Decision(
            ticker=market.ticker,
            action=action,
            side=side,
            p_yes_pooled=p_pooled,
            market_mid_cents=mid,
            price_cents=price,
            order_type=order_type,
            fee_cents_per_contract=fee_pc,
            edge_cents=edge,
            voting_count=len(active),
            abstain_count=abstain_count,
            agree_count=agree_count,
            effective_agree=effective_agree,
            quorum=cfg.quorum,
            reasons=tuple(reasons),
            vetoes=tuple(veto_results),
            votes=tuple(fresh),
            weights=weights,
            created_at=now,
        )

    # ------------------------------------------------------------------ #

    @staticmethod
    def _latest_votes(ticker: str, votes: Iterable[AgentVote]) -> List[AgentVote]:
        by_agent: Dict[str, AgentVote] = {}
        for v in votes:
            if v.ticker != ticker:
                continue
            prev = by_agent.get(v.agent)
            if prev is None or v.created_at >= prev.created_at:
                by_agent[v.agent] = v
        return sorted(by_agent.values(), key=lambda v: v.agent)

    def _weights(self, active: List[AgentVote]) -> Dict[str, float]:
        cfg = self.config
        family_size: Dict[str, int] = defaultdict(int)
        for v in active:
            family_size[v.family] += 1
        out: Dict[str, float] = {}
        for v in active:
            base = self.reliability.weight(v.agent) if self.reliability else 1.0
            conf = cfg.confidence_floor + (1.0 - cfg.confidence_floor) * v.confidence
            out[v.agent] = base * conf / (family_size[v.family] ** cfg.correlation_alpha)
        return out

    def _pool(self, active: List[AgentVote], weights: Dict[str, float], market_p: float) -> float:
        num = self.config.market_weight * _logit(market_p)
        den = self.config.market_weight
        for v in active:
            w = weights.get(v.agent, 0.0)
            num += w * _logit(float(v.p_yes))
            den += w
        if den <= 0:
            return market_p
        return _sigmoid(num / den)

    def _majority(self, active: List[AgentVote], mid_cents: float) -> Tuple[Optional[str], int, float]:
        """
        Direction by independent families.  Each family contributes at most 1.0,
        split across its members that took a side; members inside the deadband
        have no directional opinion and neither help nor dilute their family.
        (BOOK at mid + WHALES leaning YES counts as one full YES family; BOOK
        leaning NO + WHALES leaning YES counts as half a family each way.)
        """
        band = self.config.direction_deadband_cents
        sides: Dict[str, List[str]] = defaultdict(list)
        for v in active:
            p_c = float(v.p_yes) * 100.0
            if p_c > mid_cents + band:
                sides[v.family].append("yes")
            elif p_c < mid_cents - band:
                sides[v.family].append("no")
        counts = {"yes": 0, "no": 0}
        effective = {"yes": 0.0, "no": 0.0}
        for fam_sides in sides.values():
            for s in fam_sides:
                counts[s] += 1
                effective[s] += 1.0 / len(fam_sides)
        if abs(effective["yes"] - effective["no"]) < 1e-9:
            return None, 0, 0.0
        side = "yes" if effective["yes"] > effective["no"] else "no"
        return side, counts[side], effective[side]

    def _entry_quote(self, market: MarketSnapshot, side: str) -> Optional[Tuple[int, str, float]]:
        cfg = self.config
        bid, ask = market.quotes(side)
        if not 1 <= ask <= 99:
            return None
        if cfg.prefer_maker and 1 <= bid and ask - bid >= 2:
            return bid + 1, "maker", cfg.maker_rate
        return ask, "taker", cfg.taker_rate
