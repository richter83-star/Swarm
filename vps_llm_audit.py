import paramiko
import sys

HOST = "144.126.146.8"
USER = "root"
PASS = "Kosh1997!"

def run(client, cmd):
    stdin, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=30)

print("=" * 60)
print("STEP 1: Find all .db files")
print("=" * 60)
out, err = run(client, "find /root/Swarm/Swarm-Kalshi -name '*.db' 2>/dev/null")
print(out)
if err:
    print("STDERR:", err)

print("=" * 60)
print("STEP 2: central_llm_controller.db tables + schema")
print("=" * 60)
out, err = run(client, "sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db '.tables' 2>/dev/null || echo 'DB not found or error'")
print("Tables:", out)
out, err = run(client, "sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db '.schema' 2>/dev/null || echo 'DB not found or error'")
print("Schema:", out)

print("=" * 60)
print("STEP 3: LLM decisions vs P&L (guessing table/col names)")
print("=" * 60)

# Try common table names
for tbl in ["decisions", "llm_decisions", "trade_decisions", "outcomes"]:
    out, err = run(client, f"""sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT name FROM sqlite_master WHERE type='table' AND name='{tbl}';" 2>/dev/null""")
    if out.strip():
        print(f"Found table: {tbl}")
        # Try to get columns
        schema_out, _ = run(client, f"sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db '.schema {tbl}' 2>/dev/null")
        print(schema_out)

# Generic query against whatever tables exist
out, err = run(client, """sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null""")
print("All tables in central_llm_controller.db:", out)

print("=" * 60)
print("STEP 4: oracle.db trades schema")
print("=" * 60)
out, err = run(client, "sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db '.schema trades' 2>/dev/null || echo 'not found'")
print(out)

print("=" * 60)
print("STEP 5: Check all bot DBs for llm columns in trades")
print("=" * 60)
for bot in ["oracle", "sentinel", "pulse", "vanguard"]:
    db = f"/root/Swarm/Swarm-Kalshi/data/{bot}.db"
    out, err = run(client, f"sqlite3 {db} '.schema trades' 2>/dev/null | head -30")
    if out.strip():
        print(f"\n--- {bot}.db trades schema ---")
        print(out)
    else:
        print(f"\n--- {bot}.db: no trades table or not found ---")

print("=" * 60)
print("STEP 6: LLM approved breakdown per bot")
print("=" * 60)
for bot in ["oracle", "sentinel", "pulse", "vanguard"]:
    db = f"/root/Swarm/Swarm-Kalshi/data/{bot}.db"
    # Try llm_approved column
    out, err = run(client, f"""sqlite3 {db} "SELECT llm_approved, COUNT(*), ROUND(AVG(pnl),4) as avg_pnl FROM trades GROUP BY llm_approved;" 2>/dev/null""")
    if out.strip():
        print(f"\n{bot} - llm_approved breakdown:\n{out}")
    else:
        # Try llm_score
        out2, _ = run(client, f"""sqlite3 {db} "SELECT CASE WHEN llm_score > 0 THEN 'positive' ELSE 'zero/neg' END as score_bucket, COUNT(*), ROUND(AVG(pnl),4) FROM trades GROUP BY score_bucket;" 2>/dev/null""")
        if out2.strip():
            print(f"\n{bot} - llm_score breakdown:\n{out2}")
        else:
            print(f"\n{bot} - no llm columns found or no data")

print("=" * 60)
print("STEP 7: central_llm_controller.db — full decision audit")
print("=" * 60)
# Get all table names first, then query what's there
out, _ = run(client, """sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT name FROM sqlite_master WHERE type='table';" 2>/dev/null""")
tables = [t.strip() for t in out.strip().split('\n') if t.strip()]
print("Tables:", tables)

for tbl in tables:
    col_out, _ = run(client, f"sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db 'PRAGMA table_info({tbl});' 2>/dev/null")
    print(f"\n--- {tbl} columns ---\n{col_out}")
    count_out, _ = run(client, f"""sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT COUNT(*) FROM {tbl};" 2>/dev/null""")
    print(f"Row count: {count_out.strip()}")
    # Show a sample
    sample_out, _ = run(client, f"""sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT * FROM {tbl} LIMIT 3;" 2>/dev/null""")
    print(f"Sample rows:\n{sample_out}")

print("=" * 60)
print("STEP 8: LLM decision vs outcome aggregation (adaptive)")
print("=" * 60)
# Try to find any column with 'decision' or 'approved' or 'pnl' in LLM db
for tbl in tables:
    col_out, _ = run(client, f"sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db 'PRAGMA table_info({tbl});' 2>/dev/null")
    cols = [line.split('|')[1] for line in col_out.strip().split('\n') if '|' in line]
    print(f"\n{tbl} columns: {cols}")

    # Check if it has decision + outcome cols
    has_decision = any('decision' in c.lower() or 'approved' in c.lower() or 'action' in c.lower() for c in cols)
    has_pnl = any('pnl' in c.lower() or 'profit' in c.lower() or 'outcome' in c.lower() or 'result' in c.lower() for c in cols)

    if has_decision and has_pnl:
        decision_col = next(c for c in cols if 'decision' in c.lower() or 'approved' in c.lower() or 'action' in c.lower())
        pnl_col = next(c for c in cols if 'pnl' in c.lower() or 'profit' in c.lower() or 'outcome' in c.lower() or 'result' in c.lower())
        agg_out, _ = run(client, f"""sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT {decision_col}, COUNT(*) as count, ROUND(AVG({pnl_col}),4) as avg_pnl, ROUND(SUM({pnl_col}),4) as total_pnl, SUM(CASE WHEN {pnl_col}>0 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN {pnl_col}<0 THEN 1 ELSE 0 END) as losses FROM {tbl} GROUP BY {decision_col};" 2>/dev/null""")
        print(f"Decision vs P&L:\n{agg_out}")
    elif has_decision:
        decision_col = next(c for c in cols if 'decision' in c.lower() or 'approved' in c.lower() or 'action' in c.lower())
        count_out, _ = run(client, f"""sqlite3 /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db "SELECT {decision_col}, COUNT(*) FROM {tbl} GROUP BY {decision_col};" 2>/dev/null""")
        print(f"Decision counts:\n{count_out}")

print("=" * 60)
print("STEP 9: Resolved trades P&L summary per bot")
print("=" * 60)
for bot in ["oracle", "sentinel", "pulse", "vanguard"]:
    db = f"/root/Swarm/Swarm-Kalshi/data/{bot}.db"
    out, _ = run(client, f"""sqlite3 {db} "SELECT status, COUNT(*), ROUND(AVG(pnl),4), ROUND(SUM(pnl),4) FROM trades GROUP BY status;" 2>/dev/null""")
    if out.strip():
        print(f"\n{bot} - trades by status (status|count|avg_pnl|total_pnl):\n{out}")

print("=" * 60)
print("DONE")
print("=" * 60)

client.close()
