import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('vmi3134862.contaboserver.net', username='root', password='Kosh1997!')

stdin, stdout, stderr = c.exec_command('kill 960073 960089 960166 960190; sleep 3 && ps -ef | grep bot_runner | grep -v grep')
out = stdout.read().decode()
err = stderr.read().decode()

print("STDOUT:")
print(out)
print("STDERR:")
print(err)

c.close()
