"""
Per-agent reliability weights learned from resolved forecasts.

Each forecast is stored with the market's own implied probability at the
time.  An agent's skill is measured against the market, not against a coin
flip: an agent that merely echoes the price earns weight ~1.0, one that
beats the market earns more, one that is worse than the market earns less.

    skill  = (brier_market - brier_agent) / brier_market
    raw    = clamp(1 + skill_gain * skill, min_weight, max_weight)
    weight = (n * raw + prior_n * 1.0) / (n + prior_n)     # shrink toward 1.0

Agents with no resolved history get weight 1.0.

Each agent is scored once per market, on its FIRST recorded opinion: that is
the forecast available when Jev decides.  Scoring the last pre-close opinion
instead would grade agents on near-certain 99c markets, where the market's
own Brier score is ~0 and skill ratios become noise.  ``brier_floor`` bounds
the denominator for the same reason.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from consensus.schema import AgentVote, clamp_probability

_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT    NOT NULL,
    ticker      TEXT    NOT NULL,
    p_yes       REAL    NOT NULL,
    market_p    REAL    NOT NULL,
    created_at  REAL    NOT NULL,
    outcome     INTEGER,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS ix_forecasts_ticker ON forecasts(ticker);
CREATE INDEX IF NOT EXISTS ix_forecasts_agent  ON forecasts(agent, resolved_at);
"""


@dataclass(frozen=True)
class AgentStats:
    agent: str
    n: int
    brier_agent: Optional[float]
    brier_market: Optional[float]
    skill: float
    weight: float


class ReliabilityStore:
    def __init__(
        self,
        path: str = ":memory:",
        window: int = 500,
        prior_n: int = 30,
        skill_gain: float = 2.0,
        min_weight: float = 0.05,
        max_weight: float = 2.0,
        brier_floor: float = 0.02,
    ) -> None:
        self.brier_floor = float(brier_floor)
        self.window = int(window)
        self.prior_n = int(prior_n)
        self.skill_gain = float(skill_gain)
        self.min_weight = float(min_weight)
        self.max_weight = float(max_weight)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes -----------------------------------------------------------

    def record_forecast(self, vote: AgentVote, market_p: float) -> Optional[int]:
        """
        Store a non-abstaining vote alongside the market's implied probability.

        One forecast per (agent, ticker): later votes on the same market are
        ignored, so an agent polled every cycle is scored once, on the opinion
        it held when the market first came up for a decision.
        """
        if vote.abstained:
            return None
        args = (float(vote.p_yes), clamp_probability(market_p), float(vote.created_at))
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM forecasts WHERE agent = ? AND ticker = ? AND outcome IS NULL",
                (vote.agent, vote.ticker),
            ).fetchone()
            if row:
                return int(row[0])
            cur = self._conn.execute(
                "INSERT INTO forecasts (agent, ticker, p_yes, market_p, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (vote.agent, vote.ticker, *args),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def open_tickers(self) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT DISTINCT ticker FROM forecasts WHERE outcome IS NULL")]

    def record_votes(self, votes: Iterable[AgentVote], market_p: float) -> int:
        return sum(1 for v in votes if self.record_forecast(v, market_p) is not None)

    def resolve(self, ticker: str, outcome_yes: bool, resolved_at: Optional[float] = None) -> int:
        """Mark every open forecast on ``ticker`` as resolved.  Returns rows updated."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE forecasts SET outcome = ?, resolved_at = ? "
                "WHERE ticker = ? AND outcome IS NULL",
                (1 if outcome_yes else 0, float(resolved_at or time.time()), ticker),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def void(self, ticker: str) -> int:
        """Drop open forecasts on a market that will never resolve yes/no."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM forecasts WHERE ticker = ? AND outcome IS NULL", (ticker,))
            self._conn.commit()
            return int(cur.rowcount)

    # -- reads ------------------------------------------------------------

    def stats(self, agent: str) -> AgentStats:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p_yes, market_p, outcome FROM forecasts "
                "WHERE agent = ? AND outcome IS NOT NULL "
                "ORDER BY resolved_at DESC, id DESC LIMIT ?",
                (agent, self.window),
            ).fetchall()
        n = len(rows)
        if n == 0:
            return AgentStats(agent, 0, None, None, 0.0, 1.0)
        brier_agent = sum((p - o) ** 2 for p, _, o in rows) / n
        brier_market = sum((m - o) ** 2 for _, m, o in rows) / n
        skill = (brier_market - brier_agent) / max(brier_market, self.brier_floor)
        raw = min(max(1.0 + self.skill_gain * skill, self.min_weight), self.max_weight)
        weight = (n * raw + self.prior_n * 1.0) / (n + self.prior_n)
        return AgentStats(agent, n, brier_agent, brier_market, skill, weight)

    def weight(self, agent: str) -> float:
        return self.stats(agent).weight

    def weights(self, agents: Iterable[str]) -> Dict[str, float]:
        return {a: self.weight(a) for a in set(agents)}

    def all_stats(self) -> Dict[str, AgentStats]:
        with self._lock:
            agents = [r[0] for r in self._conn.execute("SELECT DISTINCT agent FROM forecasts")]
        return {a: self.stats(a) for a in agents}
