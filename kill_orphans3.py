import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('vmi3134862.contaboserver.net', username='root', password='Kosh1997!')

# Force kill with SIGKILL
stdin, stdout, stderr = c.exec_command('kill -9 960073 960089 960166 960190 2>&1; echo "kill9_done"')
out = stdout.read().decode()
print("Kill -9 output:")
print(out)

# Wait then check
stdin2, stdout2, stderr2 = c.exec_command('sleep 3 && ps -ef | grep bot_runner | grep -v grep')
out2 = stdout2.read().decode()
print("Process list after kill -9:")
print(out2)

c.close()
