import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

commands = [
    ('SWARM_LOG5_ALL_BOTS', 'grep -E "sentinel|oracle|vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -30'),
    ('SWARM_LOG5_IDLE', r'grep -E "(sentinel|oracle|vanguard).*[Ii]dle|[Ii]dle.*(sentinel|oracle|vanguard)|human_behavior.*(sentinel|oracle|vanguard)" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -20'),
    ('SWARM_LOG5_NO_PULSE_TAIL200', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | grep -v pulse'),
    ('SWARM_LOG4_ALL_BOTS', 'grep -E "sentinel|oracle|vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.4 | tail -30'),
    ('SWARM_LOG4_NO_PULSE_TAIL200', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.4 | grep -v pulse'),
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
