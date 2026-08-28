import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('vmi3134862.contaboserver.net', username='root', password='Kosh1997!')

# Check if 960068 (old daemon) is still alive
stdin, stdout, stderr = c.exec_command('ps -ef | grep 960068 | grep -v grep')
out = stdout.read().decode()
print("Checking parent 960068:")
print(out)

# Kill 980553 (new orphan under 960068) and also kill 960068 itself if alive
stdin2, stdout2, stderr2 = c.exec_command('kill -9 980553 960068 2>&1; echo done')
out2 = stdout2.read().decode()
print("Kill output:")
print(out2)

# Final state
stdin3, stdout3, stderr3 = c.exec_command('sleep 3 && ps -ef | grep bot_runner | grep -v grep')
out3 = stdout3.read().decode()
print("Final process list:")
print(out3)

c.close()
