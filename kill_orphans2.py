import paramiko
import time

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('vmi3134862.contaboserver.net', username='root', password='Kosh1997!')

# First kill
stdin, stdout, stderr = c.exec_command('kill 960073 960089 960166 960190 2>&1; echo "kill_done"')
out = stdout.read().decode()
print("Kill output:")
print(out)

# Wait then check
stdin2, stdout2, stderr2 = c.exec_command('sleep 3 && ps -ef | grep bot_runner | grep -v grep')
out2 = stdout2.read().decode()
print("Process list after kill:")
print(out2)

c.close()
