"""
DecisionLedger: append-only record of every consensus decision.

Used for shadow mode (log what Jev *would* do without trading), for the HUD,
and for the go/no-go gate: once fired decisions resolve, ``summary()``
reports realised P&L per contract with a 95% confidence interval.

P&L assumes one contract filled at the decision's price and fee.  Maker
prices are optimistic (they assume a fill); treat maker results as an upper
bound until live fills confirm them.  Statistics count only the first fired
decision per market, so re-evaluating a market every cycle cannot inflate
the sample size.

Strikes of one event (every bracket of one city's daily high, every strike of
one BTC hourly print) settle on the same number, so their outcomes are not
independent.  The confidence interval is therefore cluster-robust by event
(``ticker`` minus its last segment), and ``resolved_events`` reports how many
independent clusters the sample really has.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from consensus.schema import Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      REAL    NOT NULL,
    ticker          TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    side            TEXT,
    p_yes_pooled    REAL    NOT NULL,
    market_mid      REAL    NOT NULL,
    price_cents     INTEGER,
    order_type      TEXT,
    fee_cents       REAL    NOT NULL,
    edge_cents      REAL    NOT NULL,
    voting_count    INTEGER NOT NULL,
    agree_count     INTEGER NOT NULL,
    effective_agree REAL    NOT NULL,
    mode            TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    outcome         INTEGER,
    pnl_cents       REAL,
    resolved_at     REAL
);
CREATE INDEX IF NOT EXISTS ix_decisions_ticker ON decisions(ticker);
CREATE INDEX IF NOT EXISTS ix_decisions_action ON decisions(action, resolved_at);
CREATE INDEX IF NOT EXISTS ix_decisions_created ON decisions(created_at);
CREATE INDEX IF NOT EXISTS ix_decisions_ticker_created ON decisions(ticker, created_at);
CREATE TABLE IF NOT EXISTS agent_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    agent      TEXT    NOT NULL,
    ticker     TEXT    NOT NULL,
    latency_ms INTEGER NOT NULL,
    status     TEXT    NOT NULL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_runs ON agent_runs(agent, ts);
"""

def event_of(ticker: str) -> str:
    """Kalshi event ticker: the market ticker without its strike segment."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


# Only the FIRST fired decision per ticker counts toward P&L statistics:
# re-evaluating the same market every cycle must not multiply one outcome.
# two-sided 95% t critical values by degrees of freedom (G - 1 clusters); 1.96 beyond 30
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262,
        10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
        18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042}


def t95(df: int) -> float:
    return _T95.get(df, 1.96) if df >= 1 else float("inf")


_FIRST_FIRES = (
    "SELECT MIN(id) FROM decisions WHERE action = 'fire' {extra} GROUP BY ticker"
)


@dataclass(frozen=True)
class LedgerSummary:
    decisions: int
    markets: int
    fired: int
    vetoed: int
    held: int
    resolved_fired: int
    resolved_events: int
    wins: int
    win_rate: Optional[float]
    mean_expected_edge_cents: Optional[float]
    mean_pnl_cents: Optional[float]
    pnl_ci95_cents: Optional[tuple]
    total_pnl_cents: float

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class DecisionLedger:
    def __init__(self, path: str = ":memory:", mode: str = "shadow") -> None:
        self.mode = mode
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(self, d: Decision) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO decisions (created_at, ticker, action, side, p_yes_pooled, "
                "market_mid, price_cents, order_type, fee_cents, edge_cents, voting_count, "
                "agree_count, effective_agree, mode, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.created_at, d.ticker, d.action, d.side, d.p_yes_pooled,
                 d.market_mid_cents, d.price_cents, d.order_type,
                 d.fee_cents_per_contract, d.edge_cents, d.voting_count,
                 d.agree_count, d.effective_agree, self.mode,
                 json.dumps(d.to_dict(), default=str)),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def resolve(self, ticker: str, outcome_yes: bool, resolved_at: Optional[float] = None) -> int:
        """Settle every open decision on ``ticker``.  Fired ones get P&L per contract."""
        ts = float(resolved_at or time.time())
        outcome = 1 if outcome_yes else 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, action, side, price_cents, fee_cents FROM decisions "
                "WHERE ticker = ? AND outcome IS NULL",
                (ticker,),
            ).fetchall()
            for row_id, action, side, price, fee in rows:
                pnl = None
                if action == "fire" and side in ("yes", "no") and price is not None:
                    won = (side == "yes" and outcome == 1) or (side == "no" and outcome == 0)
                    pnl = (100.0 if won else 0.0) - float(price) - float(fee)
                self._conn.execute(
                    "UPDATE decisions SET outcome = ?, pnl_cents = ?, resolved_at = ? WHERE id = ?",
                    (outcome, pnl, ts, row_id),
                )
            self._conn.commit()
            return len(rows)

    def void(self, ticker: str, resolved_at: Optional[float] = None) -> int:
        """Retire open decisions on a market that will never pay out (void / unfetchable).
        outcome = -1 marks them closed; pnl stays NULL so they never enter statistics."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE decisions SET outcome = -1, pnl_cents = NULL, resolved_at = ? "
                "WHERE ticker = ? AND outcome IS NULL", (float(resolved_at or time.time()), ticker))
            self._conn.commit()
            return int(cur.rowcount)

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload, outcome, pnl_cents FROM decisions ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        out = []
        for payload, outcome, pnl in rows:
            d = json.loads(payload)
            d["outcome"] = outcome
            d["pnl_cents"] = pnl
            out.append(d)
        return out

    def latest_per_ticker(self, since: float, limit: int = 400) -> List[Dict[str, Any]]:
        """Each market's most recent decision since ``since``, newest first.
        One row per market, so markets the recorder samples more often don't count twice."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload, outcome, pnl_cents FROM decisions WHERE id IN "
                "(SELECT MAX(id) FROM decisions WHERE created_at >= ? GROUP BY ticker) "
                "ORDER BY id DESC LIMIT ?",
                (float(since), int(limit)),
            ).fetchall()
        out = []
        for payload, outcome, pnl in rows:
            d = json.loads(payload)
            d["outcome"] = outcome
            d["pnl_cents"] = pnl
            out.append(d)
        return out

    def record_agent_runs(self, runs: List[Dict[str, Any]]) -> None:
        if not runs:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO agent_runs (ts, agent, ticker, latency_ms, status, error) "
                "VALUES (?,?,?,?,?,?)",
                [(r["ts"], r["agent"], r["ticker"], int(r.get("latency_ms", 0)),
                  r.get("status", ""), r.get("error", "")) for r in runs],
            )
            self._conn.commit()

    def prune_agent_runs(self, older_than: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM agent_runs WHERE ts < ?", (float(older_than),))
            self._conn.commit()
            return int(cur.rowcount)

    def agent_activity(self, since: float, bucket_s: int = 3600) -> Dict[str, Dict[str, Any]]:
        """Per-agent run counts, latency, last error, and an hourly vote sparkline."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent, status, COUNT(*), AVG(latency_ms), MAX(ts) FROM agent_runs "
                "WHERE ts >= ? GROUP BY agent, status", (float(since),)).fetchall()
            errs = self._conn.execute(
                "SELECT agent, error, MAX(ts) FROM agent_runs WHERE ts >= ? AND status = 'error' "
                "GROUP BY agent", (float(since),)).fetchall()
            spark = self._conn.execute(
                "SELECT agent, CAST((ts - ?) / ? AS INTEGER) AS b, COUNT(*) FROM agent_runs "
                "WHERE ts >= ? AND status = 'vote' GROUP BY agent, b",
                (float(since), int(bucket_s), float(since))).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for agent, status, n, lat, last in rows:
            a = out.setdefault(agent, {"runs": 0, "votes": 0, "abstains": 0, "errors": 0,
                                       "latency_sum": 0.0, "last_ts": 0.0, "last_error": "",
                                       "spark": {}})
            a["runs"] += n
            a["latency_sum"] += (lat or 0.0) * n
            a["last_ts"] = max(a["last_ts"], last or 0.0)
            key = {"vote": "votes", "abstain": "abstains", "error": "errors"}.get(status)
            if key:
                a[key] += n
        for agent, err, _ in errs:
            if agent in out:
                out[agent]["last_error"] = err or ""
        for agent, b, n in spark:
            if agent in out:
                out[agent]["spark"][int(b)] = n
        for a in out.values():
            a["avg_latency_ms"] = round(a.pop("latency_sum") / a["runs"], 1) if a["runs"] else 0.0
        return out

    def hourly_counts(self, since: float, buckets: int = 24, bucket_s: int = 3600) -> Dict[str, List[int]]:
        """Decisions per action per hour bucket, oldest bucket first."""
        out = {a: [0] * buckets for a in ("fire", "hold", "veto")}
        with self._lock:
            rows = self._conn.execute(
                "SELECT action, CAST((created_at - ?) / ? AS INTEGER) AS b, COUNT(*) FROM decisions "
                "WHERE created_at >= ? GROUP BY action, b",
                (float(since), int(bucket_s), float(since))).fetchall()
        for action, b, n in rows:
            b = min(int(b), buckets - 1)          # 'now' itself lands in the last bucket
            if action in out and b >= 0:
                out[action][b] += n
        return out

    def recent_runs(self, limit: int = 60) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, agent, ticker, latency_ms, status, error FROM agent_runs "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [{"ts": r[0], "agent": r[1], "ticker": r[2], "latency_ms": r[3],
                 "status": r[4], "error": r[5] or ""} for r in rows]

    def open_tickers(self) -> List[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT DISTINCT ticker FROM decisions WHERE outcome IS NULL")]

    def pnl_curve(self) -> List[Dict[str, Any]]:
        """Cumulative P&L (cents/contract) over resolved first-fire decisions."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT resolved_at, ticker, pnl_cents FROM decisions "
                f"WHERE id IN ({_FIRST_FIRES.format(extra='')}) AND pnl_cents IS NOT NULL "
                "ORDER BY resolved_at, id"
            ).fetchall()
        total, out = 0.0, []
        for ts, ticker, pnl in rows:
            total += pnl
            out.append({"ts": ts, "ticker": ticker, "pnl": pnl, "cum": round(total, 3)})
        return out

    def first_ts(self) -> Optional[float]:
        with self._lock:
            row = self._conn.execute("SELECT MIN(created_at) FROM decisions").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def summary(self, since: Optional[float] = None) -> LedgerSummary:
        where = "WHERE created_at >= ?" if since is not None else ""
        # first fires are found over the WHOLE ledger, then filtered by date, so a market
        # that fired before ``since`` cannot count again because it re-fired after it
        after = "AND created_at >= ?" if since is not None else ""
        first = _FIRST_FIRES.format(extra="")
        args = (float(since),) if since is not None else ()
        with self._lock:
            counts = dict(self._conn.execute(
                f"SELECT action, COUNT(*) FROM decisions {where} GROUP BY action", args
            ).fetchall())
            markets = self._conn.execute(
                f"SELECT COUNT(DISTINCT ticker) FROM decisions {where}", args).fetchone()[0]
            fired_markets = self._conn.execute(
                f"SELECT COUNT(*) FROM decisions WHERE id IN ({first}) {after}", args).fetchone()[0]
            fired = self._conn.execute(
                "SELECT ticker, edge_cents, pnl_cents FROM decisions "
                f"WHERE id IN ({first}) AND pnl_cents IS NOT NULL {after}",
                args,
            ).fetchall()
        n = len(fired)
        pnls = [p for _, _, p in fired]
        wins = sum(1 for p in pnls if p > 0)
        mean_pnl = sum(pnls) / n if n else None
        clusters: Dict[str, float] = {}
        for ticker, _, p in fired:
            key = event_of(ticker)
            clusters[key] = clusters.get(key, 0.0) + (p - mean_pnl)
        g = len(clusters)
        ci = None
        if n >= 2 and g >= 2:
            # cluster-robust variance of the mean (CR1): (G/(G-1)) * sum_g (sum_i e_i)^2 / n^2
            var = (g / (g - 1)) * sum(r * r for r in clusters.values()) / (n * n)
            half = t95(g - 1) * math.sqrt(var)      # few events -> wide interval, honestly
            ci = (mean_pnl - half, mean_pnl + half)
        return LedgerSummary(
            decisions=sum(counts.values()),
            markets=int(markets),
            fired=int(fired_markets),
            vetoed=counts.get("veto", 0),
            held=counts.get("hold", 0),
            resolved_fired=n,
            resolved_events=g,
            wins=wins,
            win_rate=(wins / n) if n else None,
            mean_expected_edge_cents=(sum(e for _, e, _ in fired) / n) if n else None,
            mean_pnl_cents=mean_pnl,
            pnl_ci95_cents=ci,
            total_pnl_cents=float(sum(pnls)),
        )
