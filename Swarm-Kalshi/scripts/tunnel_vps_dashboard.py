"""
Resilient Local SSH Tunnel Daemon for Kalshi Swarm Dashboard.
Automatically reconnects on network drop or system wake-from-sleep.
Forwards local port 8888 to remote VPS port 8888 over SSH.
"""

import socket
import threading
import time
import sys
import paramiko

import os
from pathlib import Path

LOCAL_PORT = 8888
REMOTE_HOST = '127.0.0.1'
REMOTE_PORT = 8888

# Load credentials from .env if present
_env_file = Path(__file__).resolve().parent.parent / '.env'
if _env_file.exists():
    try:
        with open(_env_file, 'r', encoding='utf-8') as _fh:
            for _line in _fh:
                _line = _line.strip()
                if _line and not _line.startswith('#') and '=' in _line:
                    _k, _v = _line.split('=', 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"\''))
    except Exception:
        pass

SSH_HOST = os.environ.get('VPS_HOST', 'vmi3134862.contaboserver.net')
SSH_USER = os.environ.get('VPS_USER', 'root')
SSH_PASS = os.environ.get('VPS_PASSWORD') or os.environ.get('VPS_PASS', '')

def forward(source, destination):
    try:
        while True:
            data = source.recv(32768)
            if not data:
                break
            destination.sendall(data)
    except Exception:
        pass
    finally:
        try:
            destination.shutdown(socket.SHUT_WR)
        except Exception:
            try:
                destination.close()
            except Exception:
                pass
        try:
            source.close()
        except Exception:
            pass

class ResilientTunnel:
    def __init__(self):
        self.ssh = None
        self.transport = None
        self.lock = threading.Lock()
        self.running = True

    def connect(self):
        with self.lock:
            if self.ssh:
                try:
                    self.ssh.close()
                except Exception:
                    pass
            print(f"[{time.strftime('%X')}] Connecting SSH tunnel to {SSH_HOST}...")
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=10)
            self.transport = self.ssh.get_transport()
            # Send keep-alive packet every 60 seconds to prevent idle timeout without channel contention
            self.transport.set_keepalive(60)
            print(f"[{time.strftime('%X')}] SSH Transport connected and 60s keep-alive enabled.")

    def is_alive(self):
        return self.transport is not None and self.transport.is_active()

    def handle_client(self, client_socket):
        try:
            client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        if not self.is_alive():
            try:
                self.connect()
            except Exception as e:
                print(f"[{time.strftime('%X')}] Reconnect failed: {e}")
                client_socket.close()
                return

        try:
            remote_channel = self.transport.open_channel(
                "direct-tcpip",
                (REMOTE_HOST, REMOTE_PORT),
                client_socket.getpeername()
            )
        except Exception as e:
            print(f"[{time.strftime('%X')}] open_channel failed ({e}), attempting reconnect...")
            try:
                self.connect()
                remote_channel = self.transport.open_channel(
                    "direct-tcpip",
                    (REMOTE_HOST, REMOTE_PORT),
                    client_socket.getpeername()
                )
            except Exception as e2:
                print(f"[{time.strftime('%X')}] Reconnect retry failed: {e2}")
                client_socket.close()
                return

        if remote_channel is None:
            client_socket.close()
            return

        t1 = threading.Thread(target=forward, args=(client_socket, remote_channel), daemon=True)
        t2 = threading.Thread(target=forward, args=(remote_channel, client_socket), daemon=True)
        t1.start()
        t2.start()

    def run(self):
        while self.running:
            try:
                self.connect()
                break
            except Exception as e:
                print(f"[{time.strftime('%X')}] Initial connection failed: {e}. Retrying in 5s...")
                time.sleep(5)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', LOCAL_PORT))
        server.listen(25)
        print(f"[{time.strftime('%X')}] [RESILIENT TUNNEL READY] Listening on http://127.0.0.1:{LOCAL_PORT} -> VPS 127.0.0.1:{REMOTE_PORT}")

        while self.running:
            try:
                client, addr = server.accept()
                threading.Thread(target=self.handle_client, args=(client,), daemon=True).start()
            except Exception as e:
                print(f"[{time.strftime('%X')}] Server accept error: {e}")
                time.sleep(0.5)

if __name__ == '__main__':
    ResilientTunnel().run()
