import paramiko

host = "144.126.146.8"
user = "root"
password = "Kosh1997!"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, timeout=30)

commands = {
    "3_open_positions": """sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db "SELECT COUNT(*) as open_positions FROM trades WHERE outcome IS NULL OR outcome='';" """,
    "4_pnl_today": """sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db "SELECT COUNT(*) as trades_today, ROUND(SUM(pnl_cents)/100.0,2) as pnl_dollars FROM trades WHERE outcome IS NOT NULL AND outcome != '' AND date(timestamp) = date('now');" """,
    "4b_pnl_today_all": """sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db "SELECT outcome, COUNT(*) as cnt, SUM(pnl_cents) as total_pnl_cents FROM trades WHERE date(timestamp) = date('now') GROUP BY outcome;" """,
}

for label, cmd in commands.items():
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"{'='*60}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    print(out or "(no output)")
    if err:
        print(f"STDERR: {err}")

client.close()
