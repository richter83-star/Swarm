"""
Agent contract.

An agent looks at one market (``MarketContext``) and returns an ``Estimate``
(probability of YES) or ``None`` to abstain.  ``Agent.vote`` wraps that in an
``AgentVote`` and isolates failures: an exception becomes an abstention with
the error recorded, so one broken data source never blocks the swarm.

Families group agents that read the same underlying data.  The consensus
engine counts a family as at most one independent vote.
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from consensus.schema import AgentVote, MarketSnapshot

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")


@dataclass
class Estimate:
    p_yes: float
    confidence: float = 0.5
    rationale: str = ""
    sources: Tuple[str, ...] = ()


@dataclass
class MarketContext:
    """Everything an agent may read about one market.  Heavy data is lazy."""

    snapshot: MarketSnapshot
    raw: Dict[str, Any] = field(default_factory=dict)
    reader: Any = None                 # KalshiPublic-like
    now: float = field(default_factory=time.time)
    orderbook_data: Optional[Dict[str, List[List[float]]]] = None
    trades_data: Optional[List[Dict[str, Any]]] = None

    @property
    def ticker(self) -> str:
        return self.snapshot.ticker

    @property
    def series(self) -> str:
        return (self.snapshot.series_ticker or self.ticker.split("-")[0]).upper()

    @property
    def strike_type(self) -> str:
        return str(self.raw.get("strike_type") or "")

    @property
    def floor_strike(self) -> Optional[float]:
        v = self.raw.get("floor_strike")
        return float(v) if v not in (None, "") else None

    @property
    def cap_strike(self) -> Optional[float]:
        v = self.raw.get("cap_strike")
        return float(v) if v not in (None, "") else None

    @property
    def event_date(self) -> Optional[date]:
        """Date encoded in the ticker, e.g. KXHIGHNY-26OCT08-T79 -> 2026-10-08."""
        src = str(self.raw.get("event_ticker") or self.ticker)
        m = _DATE_RE.search(src)
        if not m:
            return None
        yy, mon, dd = m.groups()
        month = _MONTHS.get(mon)
        if not month:
            return None
        try:
            return date(2000 + int(yy), month, int(dd))
        except ValueError:
            return None

    @property
    def seconds_to_close(self) -> Optional[float]:
        h = self.snapshot.hours_to_close
        return h * 3600.0 if h is not None else None

    def orderbook(self) -> Dict[str, List[List[float]]]:
        if self.orderbook_data is None:
            self.orderbook_data = self.reader.get_orderbook(self.ticker) if self.reader else {"yes": [], "no": []}
        return self.orderbook_data

    def trades(self) -> List[Dict[str, Any]]:
        if self.trades_data is None:
            self.trades_data = self.reader.get_trades(self.ticker) if self.reader else []
        return self.trades_data


class Agent(ABC):
    name: str = "agent"
    family: str = ""
    tier: str = "cheap"          # "cheap" runs every market; "expensive" is budgeted
    label: str = ""              # HUD tile label

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = dict(config or {})
        self.enabled = bool(self.config.get("enabled", True))
        self.family = self.config.get("family") or self.family or self.name

    def applies(self, ctx: MarketContext) -> bool:
        return True

    @abstractmethod
    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        ...

    def vote(self, ctx: MarketContext) -> Tuple[AgentVote, Dict[str, Any]]:
        """Return the vote plus a run record (latency, status, error) for the HUD."""
        t0 = time.monotonic()
        status, error, est = "abstain", "", None
        try:
            if self.applies(ctx):
                est = self.estimate(ctx)
                status = "vote" if est is not None else "abstain"
        except Exception as exc:  # isolation: a broken source abstains
            status, error = "error", f"{type(exc).__name__}: {exc}"[:300]
        latency_ms = int((time.monotonic() - t0) * 1000)
        if est is not None:
            p = min(max(float(est.p_yes), 0.0), 1.0)
            v = AgentVote(agent=self.name, ticker=ctx.ticker, p_yes=p, confidence=est.confidence,
                          family=self.family, sources=tuple(est.sources), latency_ms=latency_ms,
                          rationale=est.rationale[:500], created_at=ctx.now)
        else:
            v = AgentVote(agent=self.name, ticker=ctx.ticker, p_yes=None, family=self.family,
                          latency_ms=latency_ms, rationale=error, created_at=ctx.now)
        run = {"agent": self.name, "ticker": ctx.ticker, "ts": ctx.now,
               "latency_ms": latency_ms, "status": status, "error": error}
        return v, run
