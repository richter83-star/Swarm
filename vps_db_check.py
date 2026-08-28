import paramiko

host = "144.126.146.8"
user = "root"
password = "Kosh1997!"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(host, username=user, password=password, timeout=30)

# First get the schema
print("=== oracle.db TABLE SCHEMA ===")
stdin, stdout, stderr = client.exec_command('sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db ".schema trades"', timeout=15)
schema = stdout.read().decode().strip()
err = stderr.read().decode().strip()
print(schema or "(no output)")
if err:
    print("ERR:", err)

# Also check what tables exist
print("\n=== TABLES IN oracle.db ===")
stdin, stdout, stderr = client.exec_command('sqlite3 /root/Swarm/Swarm-Kalshi/data/oracle.db ".tables"', timeout=15)
print(stdout.read().decode().strip())

client.close()
