import paramiko
import sys

host = "144.126.146.8"
user = "root"
password = "Kosh1997!"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, timeout=30)

commands = {
    "1_bot_processes": "ps -ef | grep bot_runner | grep -v grep",
    "2_recent_logs": "tail -30 /root/Swarm/Swarm-Kalshi/logs/swarm.log",
    "3_open_positions": "sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db \"SELECT COUNT(*) as open_positions FROM trades WHERE status = 'open';\"",
    "4_pnl_today": "sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db \"SELECT COUNT(*) as trades_today, ROUND(SUM(pnl),2) as pnl_today FROM trades WHERE status='resolved' AND date(created_at) = date('now');\"",
    "5_balance_grep": "grep -i 'balance' /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -5",
    "6_drawdown_cooldown": "grep -i 'drawdown\\|cooldown' /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -5",
}

for label, cmd in commands.items():
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"CMD: {cmd}")
    print(f"{'='*60}")
    stdin, stdout, stderr = client.exec_command(cmd, timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(out)
    if err:
        print(f"STDERR: {err}")
    if not out and not err:
        print("(no output)")

client.close()
print("\n\nDone.")
