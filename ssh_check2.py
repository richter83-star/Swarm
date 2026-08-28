import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

commands = [
    ('LOG_TAIL', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log'),
    ('LOG_CHECK', 'wc -l /root/Swarm/Swarm-Kalshi/logs/swarm.log && ls -lh /root/Swarm/Swarm-Kalshi/logs/swarm.log'),
    ('BOT_NAMES_CHECK', 'head -50 /root/Swarm/Swarm-Kalshi/logs/swarm.log'),
    ('GREP_BOT', 'grep -i "sentinel\\|oracle\\|vanguard\\|pulse" /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -30'),
    ('IDLE_CHECK', 'grep -i "idle\\|human_behavior\\|sleeping\\|wait" /root/Swarm/Swarm-Kalshi/logs/swarm.log | tail -20'),
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
