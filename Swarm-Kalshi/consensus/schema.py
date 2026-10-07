"""
Data contracts shared by every agent, the consensus engine, the ledger,
and the HUD.

Prices are integer cents (1-99), probabilities are floats in (0, 1).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

P_MIN = 0.01
P_MAX = 0.99


def clamp_probability(p: float) -> float:
    """Keep probabilities away from 0/1 so log-odds stay finite."""
    return min(max(float(p), P_MIN), P_MAX)


@dataclass(frozen=True)
class MarketSnapshot:
    """The minimum market state the consensus layer needs."""

    ticker: str
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    series_ticker: str = ""
    title: str = ""
    category: str = ""
    last_price: int = 0
    volume_24h: int = 0
    open_interest: int = 0
    liquidity_cents: int = 0
    hours_to_close: Optional[float] = None
    rules_text: str = ""

    @property
    def two_sided(self) -> bool:
        return 0 < self.yes_bid < self.yes_ask < 100

    @property
    def yes_spread_cents(self) -> int:
        if not self.two_sided:
            return 100
        return self.yes_ask - self.yes_bid

    @property
    def mid_cents(self) -> float:
        """YES mid price in cents; falls back to last trade when one-sided."""
        if self.two_sided:
            return (self.yes_bid + self.yes_ask) / 2.0
        if 0 < self.last_price < 100:
            return float(self.last_price)
        return 50.0

    def quotes(self, side: str) -> Tuple[int, int]:
        """(bid, ask) in cents for the requested side."""
        if side == "yes":
            return self.yes_bid, self.yes_ask
        if side == "no":
            return self.no_bid, self.no_ask
        raise ValueError(f"side must be 'yes' or 'no', got {side!r}")

    @classmethod
    def from_opportunity(cls, opp: Any, rules_text: str = "") -> "MarketSnapshot":
        """Build from ``kalshi_agent.market_scanner.MarketOpportunity`` (duck-typed)."""
        hours = getattr(opp, "hours_to_expiry", None)
        return cls(
            ticker=str(getattr(opp, "ticker", "")),
            yes_bid=int(getattr(opp, "yes_bid", 0) or 0),
            yes_ask=int(getattr(opp, "yes_ask", 0) or 0),
            no_bid=int(getattr(opp, "no_bid", 0) or 0),
            no_ask=int(getattr(opp, "no_ask", 0) or 0),
            series_ticker=str(getattr(opp, "series_ticker", "") or ""),
            title=str(getattr(opp, "title", "") or ""),
            category=str(getattr(opp, "category", "") or ""),
            last_price=int(getattr(opp, "last_price", 0) or 0),
            volume_24h=int(getattr(opp, "volume_24h", 0) or 0),
            open_interest=int(getattr(opp, "open_interest", 0) or 0),
            liquidity_cents=int(getattr(opp, "liquidity", 0) or 0),
            hours_to_close=float(hours) if hours not in (None, "") else None,
            rules_text=rules_text,
        )


@dataclass(frozen=True)
class AgentVote:
    """
    One agent's opinion on one market.

    ``p_yes=None`` means the agent abstains (no relevant data).  Abstentions
    never count toward the quorum.

    ``family`` groups agents that read the same underlying data.  Agents in
    the same family are discounted so correlated opinions are not counted as
    independent evidence.  Defaults to the agent's own name (independent).
    """

    agent: str
    ticker: str
    p_yes: Optional[float]
    confidence: float = 0.5
    family: str = ""
    sources: Tuple[str, ...] = ()
    latency_ms: int = 0
    rationale: str = ""
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("AgentVote.agent is required")
        if not self.ticker:
            raise ValueError("AgentVote.ticker is required")
        if self.p_yes is not None:
            p = float(self.p_yes)
            if not 0.0 <= p <= 1.0:
                raise ValueError(f"p_yes must be in [0, 1], got {p}")
            object.__setattr__(self, "p_yes", clamp_probability(p))
        c = float(self.confidence)
        object.__setattr__(self, "confidence", min(max(c, 0.0), 1.0))
        if not self.family:
            object.__setattr__(self, "family", self.agent)
        object.__setattr__(self, "sources", tuple(self.sources))

    @property
    def abstained(self) -> bool:
        return self.p_yes is None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sources"] = list(self.sources)
        return d


@dataclass(frozen=True)
class VetoResult:
    name: str
    blocked: bool
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """
    Output of the consensus engine for one market.

    ``action`` is one of:
      * ``"fire"`` – every gate passed; a trade may be placed.
      * ``"hold"`` – no veto, but quorum or edge was not met.
      * ``"veto"`` – a deterministic rule blocked the market.
    """

    ticker: str
    action: str
    side: Optional[str]
    p_yes_pooled: float
    market_mid_cents: float
    price_cents: Optional[int]
    order_type: Optional[str]
    fee_cents_per_contract: float
    edge_cents: float
    voting_count: int
    abstain_count: int
    agree_count: int
    effective_agree: float
    quorum: float
    reasons: Tuple[str, ...]
    vetoes: Tuple[VetoResult, ...]
    votes: Tuple[AgentVote, ...]
    weights: Dict[str, float]
    created_at: float = field(default_factory=time.time)

    @property
    def fired(self) -> bool:
        return self.action == "fire"

    @property
    def p_side(self) -> Optional[float]:
        if self.side == "yes":
            return self.p_yes_pooled
        if self.side == "no":
            return 1.0 - self.p_yes_pooled
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "p_yes_pooled": round(self.p_yes_pooled, 4),
            "market_mid_cents": round(self.market_mid_cents, 2),
            "price_cents": self.price_cents,
            "order_type": self.order_type,
            "fee_cents_per_contract": round(self.fee_cents_per_contract, 4),
            "edge_cents": round(self.edge_cents, 3),
            "voting_count": self.voting_count,
            "abstain_count": self.abstain_count,
            "agree_count": self.agree_count,
            "effective_agree": round(self.effective_agree, 3),
            "quorum": self.quorum,
            "reasons": list(self.reasons),
            "vetoes": [v.to_dict() for v in self.vetoes],
            "votes": [v.to_dict() for v in self.votes],
            "weights": {k: round(w, 4) for k, w in self.weights.items()},
            "created_at": self.created_at,
        }


def blocked_vetoes(results: List[VetoResult]) -> List[VetoResult]:
    return [r for r in results if r.blocked]
