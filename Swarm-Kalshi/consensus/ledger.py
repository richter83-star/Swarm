"""
DecisionLedger: append-only record of every consensus decision.

Used for shadow mode (log what Jev *would* do without trading), for the HUD,
and for the go/no-go gate: once fired decisions resolve, ``summary()``
reports realised P&L per contract with a 95% confidence interval.

P&L assumes one contract filled at the decision's price and fee.  Maker
prices are optimistic (they assume a fill); treat maker results as an upper
bound until live fills confirm them.
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
"""


@dataclass(frozen=True)
class LedgerSummary:
    decisions: int
    fired: int
    vetoed: int
    held: int
    resolved_fired: int
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

    def summary(self, since: Optional[float] = None) -> LedgerSummary:
        where = "WHERE created_at >= ?" if since is not None else ""
        args = (float(since),) if since is not None else ()
        with self._lock:
            counts = dict(self._conn.execute(
                f"SELECT action, COUNT(*) FROM decisions {where} GROUP BY action", args
            ).fetchall())
            fired = self._conn.execute(
                "SELECT edge_cents, pnl_cents FROM decisions "
                f"{where + ' AND' if where else 'WHERE'} action = 'fire' AND pnl_cents IS NOT NULL",
                args,
            ).fetchall()
        n = len(fired)
        pnls = [p for _, p in fired]
        wins = sum(1 for p in pnls if p > 0)
        mean_pnl = sum(pnls) / n if n else None
        ci = None
        if n >= 2:
            var = sum((p - mean_pnl) ** 2 for p in pnls) / (n - 1)
            half = 1.96 * math.sqrt(var / n)
            ci = (mean_pnl - half, mean_pnl + half)
        return LedgerSummary(
            decisions=sum(counts.values()),
            fired=counts.get("fire", 0),
            vetoed=counts.get("veto", 0),
            held=counts.get("hold", 0),
            resolved_fired=n,
            wins=wins,
            win_rate=(wins / n) if n else None,
            mean_expected_edge_cents=(sum(e for e, _ in fired) / n) if n else None,
            mean_pnl_cents=mean_pnl,
            pnl_ci95_cents=ci,
            total_pnl_cents=float(sum(pnls)),
        )
