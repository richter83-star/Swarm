import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

commands = [
    ('LIST_LOGS', 'ls -lh /root/Swarm/Swarm-Kalshi/logs/'),
    ('PS_BOTS', 'ps -ef | grep bot_runner | grep -v grep'),
    ('PS_ALL_PYTHON', 'ps -ef | grep python | grep -v grep'),
    ('SENTINEL_LOG', 'ls -lh /root/Swarm/Swarm-Kalshi/logs/ | grep sentinel; tail -30 /root/Swarm/Swarm-Kalshi/logs/sentinel*.log 2>/dev/null || echo "no sentinel log"'),
    ('ORACLE_LOG', 'tail -30 /root/Swarm/Swarm-Kalshi/logs/oracle*.log 2>/dev/null || echo "no oracle log"'),
    ('VANGUARD_LOG', 'tail -30 /root/Swarm/Swarm-Kalshi/logs/vanguard*.log 2>/dev/null || echo "no vanguard log"'),
    ('DAEMON_LOG', 'tail -30 /root/Swarm/Swarm-Kalshi/logs/daemon*.log 2>/dev/null || echo "no daemon log"'),
    ('ALL_LOGS', 'ls -lh /root/Swarm/Swarm-Kalshi/logs/'),
]

for label, cmd in commands:
    print(f'\n{"="*60}')
    print(f'### {label}')
    print('='*60)
    stdin, stdout, stderr = c.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out:
        print(out)
    else:
        print('(no output)')
    if err:
        print('STDERR:', err)

c.close()
print('\nDONE')
