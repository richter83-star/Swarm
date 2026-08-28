import paramiko
import time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('vmi3134862.contaboserver.net', username='root', password='Kosh1997!', timeout=30)

def run(cmd):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=60)
    out = stdout.read().decode()
    err = stderr.read().decode()
    return (out + err).strip()

# Kill daemon by PID first (prevents auto-revival), then kill bots
print('=== killing daemon + bots ===')
print(run('kill 1204490 2>/dev/null; sleep 2; kill -9 1204537 1204560 1204564 1204593 2>/dev/null; sleep 3; echo done'))

print('\n=== verify stopped ===')
print(run('ps -ef | grep -E "bot_runner|swarm_daemon" | grep -v grep || echo "all stopped"'))

print('\n=== starting swarm ===')
print(run('cd /root/Swarm/Swarm-Kalshi && bash launch.sh'))
time.sleep(8)

print('\n=== bot processes ===')
print(run('ps -ef | grep bot_runner | grep -v grep'))

client.close()
