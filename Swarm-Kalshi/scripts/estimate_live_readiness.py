import sqlite3
from pathlib import Path
from datetime import datetime, timezone
import json

data_dir = Path("data")

print("=== 1. CENTRAL LLM DECISION PIPELINE ===")
llm_db = data_dir / "central_llm_controller.db"
if llm_db.exists():
    conn = sqlite3.connect(llm_db)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) as c FROM llm_decisions").fetchone()["c"]
    approved = conn.execute("SELECT COUNT(*) as c FROM llm_decisions WHERE decision='approve'").fetchone()["c"]
    rejected = conn.execute("SELECT COUNT(*) as c FROM llm_decisions WHERE decision='reject'").fetchone()["c"]
    
    first_row = conn.execute("SELECT timestamp FROM llm_decisions ORDER BY id ASC LIMIT 1").fetchone()
    last_row = conn.execute("SELECT timestamp FROM llm_decisions ORDER BY id DESC LIMIT 1").fetchone()
    
    first_ts = datetime.fromisoformat(first_row["timestamp"]) if first_row else datetime.now(timezone.utc)
    last_ts = datetime.fromisoformat(last_row["timestamp"]) if last_row else datetime.now(timezone.utc)
    hours_elapsed = max(0.1, (last_ts - first_ts).total_seconds() / 3600.0)
    eval_rate_per_hour = total / hours_elapsed
    
    print(f"Total Decisions Evaluated: {total}")
    print(f"Approved: {approved} ({(approved/total*100):.1f}%) | Rejected: {rejected}")
    print(f"Time Span: {hours_elapsed:.2f} hours")
    print(f"Evaluation Rate: ~{eval_rate_per_hour:.1f} decisions/hour")
    
    cols = [col[1] for col in conn.execute("PRAGMA table_info(llm_decisions)").fetchall()]
    print(f"Columns in llm_decisions: {cols}")
    recent = conn.execute("SELECT * FROM llm_decisions ORDER BY id DESC LIMIT 5").fetchall()
    print("\nRecent Decisions:")
    for r in recent:
        d = dict(r)
        print(f"  [{d.get('timestamp')}] {d.get('ticker')} | Decision: {d.get('decision')} | Conf: {d.get('confidence')} | Price: {d.get('suggested_price')}")
    conn.close()

print("\n=== 2. BOT TRADE DATABASES & SETTLED TRADES ===")
for bot in ["sentinel", "oracle", "pulse", "vanguard"]:
    bot_db = data_dir / f"{bot}.db"
    if bot_db.exists():
        conn = sqlite3.connect(bot_db)
        conn.row_factory = sqlite3.Row
        trades = conn.execute("SELECT * FROM trades").fetchall()
        print(f"Bot {bot:10s} -> Total Logged Trades: {len(trades)}")
        conn.close()

print("\n=== 3. META LEARNING & CATEGORY ADAPTATION ===")
meta_db = data_dir / "meta_learning.db"
if meta_db.exists():
    conn = sqlite3.connect(meta_db)
    conn.row_factory = sqlite3.Row
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"Meta Learning Tables: {[t['name'] for t in tables]}")
    conn.close()
