"""
Backfill historical trade rows against P&L invariants.

By default this runs in dry-run mode and reports anomalies.
Use --apply to persist quarantine updates.

Examples:
    python tools/backfill_pnl_invariants.py
    python tools/backfill_pnl_invariants.py --apply
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _validate(count: int, entry_price: int, pnl_cents: int, tolerance_pct: float = 0.05) -> Tuple[bool, str, Dict[str, int]]:
    count_i = max(1, int(count or 1))
    entry_i = max(0, int(entry_price or 0))
    pnl_i = int(pnl_cents or 0)
    max_loss = count_i * entry_i
    max_gain = count_i * max(0, 100 - entry_i)
    tol = max(5, int(max(max_loss, max_gain) * tolerance_pct))
    min_allowed = -max_loss - tol
    max_allowed = max_gain + tol
    trace = {
        "count": count_i,
        "entry_price_cents": entry_i,
        "pnl_cents": pnl_i,
        "max_theoretical_loss_cents": -max_loss,
        "max_theoretical_gain_cents": max_gain,
        "tolerance_cents": tol,
        "min_allowed_cents": min_allowed,
        "max_allowed_cents": max_allowed,
    }
    if pnl_i < min_allowed:
        return False, f"loss exceeds theoretical bound ({pnl_i} < {min_allowed})", trace
    if pnl_i > max_allowed:
        return False, f"gain exceeds theoretical bound ({pnl_i} > {max_allowed})", trace
    return True, "ok", trace


def _ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    additions = {
        "pnl_valid": "INTEGER DEFAULT 1",
        "pnl_validation_reason": "TEXT",
        "reconciliation_trace": "TEXT",
    }
    for col, ddl in additions.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")
    conn.commit()


def process_db(db_path: Path, apply: bool) -> Dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        has_trades = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()[0]
        if not has_trades:
            return {"db": db_path.name, "scanned": 0, "invalid": 0, "updated": 0}

        _ensure_columns(conn)
        rows = conn.execute(
            """
            SELECT id, ticker, count, entry_price, pnl_cents, outcome, bot_name
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND pnl_cents IS NOT NULL
            ORDER BY id
            """
        ).fetchall()

        invalid_rows: List[Tuple[int, str, str, str]] = []
        for r in rows:
            ok, reason, trace = _validate(
                int(r["count"] or 0),
                int(r["entry_price"] or 0),
                int(r["pnl_cents"] or 0),
            )
            if ok:
                continue
            trace_blob = json.dumps(
                {
                    **trace,
                    "ticker": str(r["ticker"] or ""),
                    "source": "historical_backfill",
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            invalid_rows.append((int(r["id"]), "pnl_invalid", reason, trace_blob))

        updated = 0
        if apply and invalid_rows:
            conn.executemany(
                """
                UPDATE trades
                SET outcome = ?,
                    pnl_valid = 0,
                    pnl_validation_reason = ?,
                    reconciliation_trace = ?
                WHERE id = ?
                """,
                [(outcome, reason, trace_blob, row_id) for row_id, outcome, reason, trace_blob in invalid_rows],
            )
            conn.commit()
            updated = len(invalid_rows)

        return {
            "db": db_path.name,
            "scanned": len(rows),
            "invalid": len(invalid_rows),
            "updated": updated,
            "samples": [
                {
                    "id": row_id,
                    "reason": reason,
                }
                for row_id, _outcome, reason, _trace in invalid_rows[:5]
            ],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill historical P&L invariants.")
    parser.add_argument("--apply", action="store_true", help="Persist quarantine updates to DB.")
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT / "data"), help="Data directory containing *.db files.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    db_files = sorted(Path(p) for p in glob.glob(str(data_dir / "*.db")))
    if not db_files:
        print(f"No DB files found in {data_dir}")
        return 1

    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    total_scanned = 0
    total_invalid = 0
    total_updated = 0
    for db in db_files:
        if db.name in {"central_llm_controller.db", "conflict_claims.db"}:
            continue
        result = process_db(db, apply=args.apply)
        total_scanned += int(result["scanned"])
        total_invalid += int(result["invalid"])
        total_updated += int(result["updated"])
        print(
            f"- {result['db']}: scanned={result['scanned']} invalid={result['invalid']} updated={result['updated']}"
        )
        for sample in result.get("samples", []):
            print(f"  sample id={sample['id']} reason={sample['reason']}")

    print("")
    print(
        f"Summary: scanned={total_scanned} invalid={total_invalid} updated={total_updated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
