import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

# The active log is swarm.log (only pulse right now due to log rotation)
# The most recent log with all 4 bots is swarm.log.5 (updated 20:21)
# and swarm.log.4 (updated 20:17)
# Run the exact original commands but on the correct log files

commands = [
    # CMD1 original - on the newest log that has all bots
    ('CMD1_on_log5', 'grep -E "sentinel|oracle|vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -30'),
    # CMD2 original - idle check on log5
    ('CMD2_on_log5', r'grep -E "(sentinel|oracle|vanguard).*[Ii]dle|[Ii]dle.*(sentinel|oracle|vanguard)|human_behavior.*sentinel|human_behavior.*oracle|human_behavior.*vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -20'),
    # CMD3 original - tail 200 no pulse on log5
    ('CMD3_on_log5', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | grep -v pulse'),
    # Also check log4 tail
    ('CMD3_on_log4', 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.4 | grep -v pulse'),
]

for label, cmd in commands:
    print(f'\n{"="*60}')
    print(f'### {label}')
    print('='*60)
    stdin, stdout, stderr = c.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out.strip():
        print(out)
    else:
        print('(no output)')
    if err.strip():
        print('STDERR:', err)

c.close()
print('\nDONE')
