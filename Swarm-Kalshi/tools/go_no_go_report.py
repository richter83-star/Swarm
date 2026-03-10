"""
Daily KPI gate report for 30-day GO/HOLD/NO-GO operation.

Examples:
    python tools/go_no_go_report.py
    python tools/go_no_go_report.py --window-days 30 --phase final
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass
class Metrics:
    settled: int = 0
    wins: int = 0
    pnl: int = 0
    gross_win: int = 0
    gross_loss: int = 0
    pending_exposure: int = 0
    pnl_invalid: int = 0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.settled * 100.0) if self.settled else 0.0

    @property
    def expectancy(self) -> float:
        return (self.pnl / self.settled) if self.settled else 0.0

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_win > 0 else 0.0
        return self.gross_win / self.gross_loss


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r[1]) == col for r in rows)


def _window_metrics(db_path: Path, cutoff: str) -> Metrics:
    m = Metrics()
    conn = sqlite3.connect(str(db_path))
    try:
        has_pnl_valid = _has_col(conn, "trades", "pnl_valid")

        valid_clause = "AND COALESCE(pnl_valid, 1) = 1" if has_pnl_valid else ""
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS n,
                COALESCE(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(pnl_cents), 0) AS pnl,
                COALESCE(SUM(CASE WHEN pnl_cents > 0 THEN pnl_cents ELSE 0 END), 0) AS gross_win,
                COALESCE(SUM(CASE WHEN pnl_cents < 0 THEN -pnl_cents ELSE 0 END), 0) AS gross_loss
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(settled_at, timestamp) >= ?
              {valid_clause}
            """,
            (cutoff,),
        ).fetchone()
        m.settled = int((row[0] if row else 0) or 0)
        m.wins = int((row[1] if row else 0) or 0)
        m.pnl = int((row[2] if row else 0) or 0)
        m.gross_win = int((row[3] if row else 0) or 0)
        m.gross_loss = int((row[4] if row else 0) or 0)

        row = conn.execute(
            """
            SELECT COALESCE(SUM(count * entry_price), 0)
            FROM trades
            WHERE outcome='pending'
            """
        ).fetchone()
        m.pending_exposure = int((row[0] if row else 0) or 0)

        if has_pnl_valid:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM trades
                WHERE (outcome='pnl_invalid' OR COALESCE(pnl_valid,1)=0)
                  AND COALESCE(settled_at, timestamp) >= ?
                """,
                (cutoff,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM trades
                WHERE outcome='pnl_invalid'
                  AND COALESCE(settled_at, timestamp) >= ?
                """,
                (cutoff,),
            ).fetchone()
        m.pnl_invalid = int((row[0] if row else 0) or 0)
    finally:
        conn.close()
    return m


def _load_guard(path: Path) -> Tuple[bool, str, int]:
    if not path.exists():
        return False, "missing", 0
    try:
        d = json.load(open(path, "r", encoding="utf-8"))
    except Exception as exc:
        return False, f"unreadable:{exc}", 0
    valid = bool(d.get("valid", False))
    reason = str(d.get("reason", ""))
    bal = int((d.get("metrics", {}) or {}).get("total_balance_cents", 0) or 0)
    return valid, reason, bal


def _phase_gate(phase: str, m: Metrics, guard_valid: bool) -> Tuple[str, str]:
    if not guard_valid or m.pnl_invalid > 0:
        return "NO-GO", "reliability_failure"

    phase = phase.lower().strip()
    if phase in {"day7", "week1"}:
        if m.settled < 8:
            return "HOLD", "insufficient_sample"
        if m.expectancy >= 0 and m.win_rate >= 50:
            return "GO", "week1_green"
        return "HOLD", "week1_mixed"

    if phase in {"day21", "week3"}:
        if m.settled < 25:
            return "HOLD", "insufficient_sample"
        if m.win_rate >= 52 and m.expectancy >= 0.5 and m.profit_factor >= 1.05:
            return "GO", "week3_green"
        if m.win_rate < 48 or m.expectancy < -1.0 or m.profit_factor < 0.90:
            return "NO-GO", "week3_red"
        return "HOLD", "week3_mixed"

    # final (day30)
    if m.settled < 50:
        return "HOLD", "insufficient_sample"
    if m.win_rate >= 53 and m.expectancy >= 1.5 and m.profit_factor >= 1.15:
        return "GO", "final_green"
    return "NO-GO", "final_kpi_miss"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate 30-day GO/HOLD/NO-GO KPI report.")
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--phase", choices=["day7", "day21", "final"], default="day7")
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"))
    args = parser.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, args.window_days))).isoformat()
    data_dir = Path(args.data_dir)
    db_files = sorted(Path(p) for p in glob.glob(str(data_dir / "*.db")))

    agg = Metrics()
    for db in db_files:
        if db.name in {"central_llm_controller.db", "conflict_claims.db"}:
            continue
        conn = sqlite3.connect(str(db))
        try:
            has_trades = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='trades'"
            ).fetchone()[0]
        finally:
            conn.close()
        if not has_trades:
            continue
        m = _window_metrics(db, cutoff)
        agg.settled += m.settled
        agg.wins += m.wins
        agg.pnl += m.pnl
        agg.gross_win += m.gross_win
        agg.gross_loss += m.gross_loss
        agg.pending_exposure += m.pending_exposure
        agg.pnl_invalid += m.pnl_invalid

    guard_valid, guard_reason, guard_balance = _load_guard(data_dir / "swarm_trade_guard.json")
    decision, reason = _phase_gate(args.phase, agg, guard_valid)

    pf = agg.profit_factor
    pf_txt = "inf" if math.isinf(pf) else f"{pf:.2f}"
    print(f"window_days={args.window_days} phase={args.phase}")
    print(f"guard_valid={guard_valid} guard_reason={guard_reason} guard_balance_cents={guard_balance}")
    print(f"settled_trades={agg.settled}")
    print(f"win_rate_pct={agg.win_rate:.2f}")
    print(f"expectancy_cents={agg.expectancy:.2f}")
    print(f"profit_factor={pf_txt}")
    print(f"pnl_cents={agg.pnl}")
    print(f"pending_exposure_cents={agg.pending_exposure}")
    print(f"pnl_invalid_count={agg.pnl_invalid}")
    print(f"decision={decision} reason={reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
