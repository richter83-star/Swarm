import paramiko
import time

host = "144.126.146.8"
user = "root"
password = "Kosh1997!"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, timeout=15)

# Step 1: kill the process
stdin, stdout, stderr = client.exec_command("kill -9 3941165 2>&1; echo 'kill_done'")
print("KILL OUTPUT:", stdout.read().decode())

# Step 2: wait 15 seconds
print("Waiting 15 seconds...")
time.sleep(15)

# Step 3: ps output
stdin, stdout, stderr = client.exec_command("ps -ef | grep bot_runner | grep -v grep")
ps_out = stdout.read().decode()
print("=== PS OUTPUT ===")
print(ps_out if ps_out else "(no bot_runner processes found)")

# Step 4: tail logs
stdin, stdout, stderr = client.exec_command("tail -20 /root/Swarm/Swarm-Kalshi/logs/swarm.log")
log_out = stdout.read().decode()
print("=== LAST 20 LOG LINES ===")
print(log_out)

client.close()
