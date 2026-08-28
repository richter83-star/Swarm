import paramiko

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

DB_BASE = "/root/Swarm/Swarm-Kalshi/data"

print("=" * 60)
print("A) llm_decisions table — row count & sequence check")
print("=" * 60)
# Confirm empty
out, _ = run(client, f'sqlite3 {DB_BASE}/central_llm_controller.db "SELECT COUNT(*) FROM llm_decisions;"')
print(f"llm_decisions rows: {out.strip()}")
out, _ = run(client, f'sqlite3 {DB_BASE}/central_llm_controller.db "SELECT * FROM sqlite_sequence;"')
print(f"sqlite_sequence: {out.strip()}")

# Check if there's a WAL or journal file (data might be uncommitted)
out, _ = run(client, "ls -lh /root/Swarm/Swarm-Kalshi/data/central_llm_controller.db*")
print(f"DB files:\n{out.strip()}")

print()
print("=" * 60)
print("B) meta_learning.db — full schema + row counts")
print("=" * 60)
out, _ = run(client, f'sqlite3 {DB_BASE}/meta_learning.db ".schema"')
print(out)
out, _ = run(client, f'sqlite3 {DB_BASE}/meta_learning.db "SELECT name FROM sqlite_master WHERE type=\'table\';"')
tables = [t.strip() for t in out.strip().split('\n') if t.strip()]
print(f"Tables: {tables}")
for tbl in tables:
    cnt, _ = run(client, f'sqlite3 {DB_BASE}/meta_learning.db "SELECT COUNT(*) FROM {tbl};"')
    sample, _ = run(client, f'sqlite3 {DB_BASE}/meta_learning.db "SELECT * FROM {tbl} LIMIT 5;"')
    print(f"\n{tbl}: {cnt.strip()} rows")
    if sample.strip():
        print(f"Sample:\n{sample.strip()}")

print()
print("=" * 60)
print("C) vanguard_trades.db — schema + row counts")
print("=" * 60)
out, _ = run(client, f'sqlite3 {DB_BASE}/vanguard_trades.db ".schema"')
print(out)
out, _ = run(client, f'sqlite3 {DB_BASE}/vanguard_trades.db "SELECT name FROM sqlite_master WHERE type=\'table\';"')
tables = [t.strip() for t in out.strip().split('\n') if t.strip()]
for tbl in tables:
    cnt, _ = run(client, f'sqlite3 {DB_BASE}/vanguard_trades.db "SELECT COUNT(*) FROM {tbl};"')
    sample, _ = run(client, f'sqlite3 {DB_BASE}/vanguard_trades.db "SELECT * FROM {tbl} LIMIT 3;"')
    print(f"\n{tbl}: {cnt.strip()} rows")
    if sample.strip():
        print(f"Sample:\n{sample.strip()}")

print()
print("=" * 60)
print("D) All bot DBs — resolved trades with P&L")
print("=" * 60)
for bot in ["oracle", "sentinel", "pulse", "vanguard"]:
    db = f"{DB_BASE}/{bot}.db"
    out, _ = run(client, f'sqlite3 {db} "SELECT outcome, COUNT(*), ROUND(AVG(pnl_cents),1), SUM(pnl_cents) FROM trades GROUP BY outcome;"')
    print(f"\n{bot} (outcome|count|avg_pnl_cents|total_pnl_cents):")
    print(out.strip() if out.strip() else "  no data")

    # Total rows
    cnt, _ = run(client, f'sqlite3 {db} "SELECT COUNT(*) FROM trades;"')
    print(f"  Total rows: {cnt.strip()}")

print()
print("=" * 60)
print("E) All bot DBs — recent trades sample (last 5)")
print("=" * 60)
for bot in ["oracle", "sentinel", "pulse", "vanguard"]:
    db = f"{DB_BASE}/{bot}.db"
    out, _ = run(client, f'sqlite3 {db} "SELECT id,timestamp,ticker,side,outcome,pnl_cents,confidence FROM trades ORDER BY id DESC LIMIT 5;"')
    print(f"\n{bot} recent trades:")
    print(out.strip() if out.strip() else "  no data")

print()
print("=" * 60)
print("F) dashboard.db — schema + data")
print("=" * 60)
out, _ = run(client, f'sqlite3 {DB_BASE}/dashboard.db ".schema"')
print(out)
out, _ = run(client, f'sqlite3 {DB_BASE}/dashboard.db "SELECT name FROM sqlite_master WHERE type=\'table\';"')
tables = [t.strip() for t in out.strip().split('\n') if t.strip()]
for tbl in tables:
    cnt, _ = run(client, f'sqlite3 {DB_BASE}/dashboard.db "SELECT COUNT(*) FROM {tbl};"')
    sample, _ = run(client, f'sqlite3 {DB_BASE}/dashboard.db "SELECT * FROM {tbl} LIMIT 3;"')
    print(f"\n{tbl}: {cnt.strip()} rows")
    if sample.strip():
        print(f"Sample:\n{sample.strip()}")

print()
print("=" * 60)
print("G) conflict_claims.db — schema + data")
print("=" * 60)
out, _ = run(client, f'sqlite3 {DB_BASE}/conflict_claims.db ".schema"')
print(out)
out, _ = run(client, f'sqlite3 {DB_BASE}/conflict_claims.db "SELECT name FROM sqlite_master WHERE type=\'table\';"')
tables = [t.strip() for t in out.strip().split('\n') if t.strip()]
for tbl in tables:
    cnt, _ = run(client, f'sqlite3 {DB_BASE}/conflict_claims.db "SELECT COUNT(*) FROM {tbl};"')
    print(f"\n{tbl}: {cnt.strip()} rows")

print()
print("=" * 60)
print("H) central_llm_controller.db — check if rows exist with different query")
print("=" * 60)
# Maybe table is not literally empty, try without WHERE
out, _ = run(client, f'sqlite3 {DB_BASE}/central_llm_controller.db "SELECT decision, executed, outcome, pnl_cents FROM llm_decisions LIMIT 10;"')
print(f"Direct select:\n{out.strip()}")
out, _ = run(client, f'sqlite3 {DB_BASE}/central_llm_controller.db "SELECT COUNT(*) FROM llm_decisions WHERE executed=1;"')
print(f"Executed=1 count: {out.strip()}")
out, _ = run(client, f'sqlite3 {DB_BASE}/central_llm_controller.db "SELECT COUNT(*) FROM llm_decisions WHERE outcome IS NOT NULL;"')
print(f"With outcome count: {out.strip()}")

print()
print("=" * 60)
print("I) Check logs for LLM gate activity")
print("=" * 60)
out, _ = run(client, "grep -i 'llm.*decision\\|llm.*approved\\|llm.*blocked\\|central_llm' /root/Swarm/Swarm-Kalshi/logs/swarm.log 2>/dev/null | tail -30")
print(out.strip() if out.strip() else "No LLM log entries found")

print()
print("=" * 60)
print("J) What's actually in the swarm log recently?")
print("=" * 60)
out, _ = run(client, "tail -50 /root/Swarm/Swarm-Kalshi/logs/swarm.log 2>/dev/null")
print(out.strip() if out.strip() else "No log or empty")

print()
print("DONE")
client.close()
