import paramiko

host = 'vmi3134862.contaboserver.net'
user = 'root'
password = 'Kosh1997!'

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(host, username=user, password=password, timeout=15)

results = {}

commands = {
    'CMD1': 'grep -E "sentinel|oracle|vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -30',
    'CMD2': r'grep -E "(sentinel|oracle|vanguard).*[Ii]dle|[Ii]dle.*(sentinel|oracle|vanguard)|human_behavior.*sentinel|human_behavior.*oracle|human_behavior.*vanguard" /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | tail -20',
    'CMD3_log5': 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.5 | grep -v pulse',
    'CMD3_log4': 'tail -200 /root/Swarm/Swarm-Kalshi/logs/swarm.log.4 | grep -v pulse',
}

for label, cmd in commands.items():
    stdin, stdout, stderr = c.exec_command(cmd)
    out = stdout.read().decode()
    err = stderr.read().decode()
    results[label] = out if out.strip() else '(no output)'

c.close()

# Save to file
with open('D:/kalshi-swarm-new/ssh_results.txt', 'w', encoding='utf-8') as f:
    for label, output in results.items():
        f.write(f'\n{"="*60}\n### {label}\n{"="*60}\n')
        f.write(output)
        f.write('\n')

print("Saved to D:/kalshi-swarm-new/ssh_results.txt")
for label, output in results.items():
    lines = output.strip().split('\n')
    print(f"\n=== {label} ({len(lines)} lines) ===")
    # print last 5 lines as preview
    for line in lines[-5:]:
        print(line)
