import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

commands = [
    ('CMD1', 'grep -E "sentinel|oracle|vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -30'),
    ('CMD2', r'grep -E "(sentinel|oracle|vanguard).*[Ii]dle|[Ii]dle.*(sentinel|oracle|vanguard)|human_behavior.*sentinel|human_behavior.*oracle|human_behavior.*vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -20'),
    ('CMD3', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log | grep -v pulse'),
    ('CMD4', 'for bot in sentinel oracle pulse vanguard; do echo "==$bot=="; cat /root/Swarm/Swarm-Kalshi/data/${bot}_risk_state.json 2>/dev/null || echo "NOT FOUND"; done'),
]

for label, cmd in commands:
    print(f'\n{"="*60}')
    print(f'### {label}: {cmd[:80]}')
    print('='*60)
    stdin, stdout, stderr = c.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    print(out)
    if err:
        print('STDERR:', err)

c.close()
print('\nDONE')
