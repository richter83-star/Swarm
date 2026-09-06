"""
Resilient Local SSH Tunnel Daemon for Kalshi Swarm Dashboard.
=============================================================
Provides a self-healing, immortal local tunnel forwarding 127.0.0.1:8888
to the 24/7 VPS Kalshi Swarm Dashboard.

Key Features:
- Supervised process watchdog (auto-restarts on network drop, WiFi switch, or sleep/wake).
- Prioritizes high-throughput native OpenSSH (ssh.exe) with aggressive keep-alive.
- Falls back to Paramiko Python transport if native OpenSSH is unavailable.
- Active HTTP health probe (recycles frozen/zombie connections within 30-45s).
- Pythonw-safe logging to logs/tunnel.log (rotating, max 5MB).
- Single-instance lock to prevent port collisions.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import logging
import signal
import subprocess
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR = PROJECT_ROOT / "data"
PID_FILE = DATA_DIR / "tunnel.pid"
LOG_FILE = LOGS_DIR / "tunnel.log"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Set up rotating logger
logger = logging.getLogger("KalshiTunnel")
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

if sys.stdout is not None:
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


# Redirect raw stdout/stderr to logger if running in pythonw
class _LogStream:
    def __init__(self, level: int):
        self.level = level

    def write(self, msg: str):
        msg = msg.strip()
        if msg:
            logger.log(self.level, msg)

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _LogStream(logging.INFO)  # type: ignore[assignment]
if sys.stderr is None:
    sys.stderr = _LogStream(logging.ERROR)  # type: ignore[assignment]

# Load credentials from .env
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        with open(_env_file, "r", encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))
    except Exception as exc:
        logger.warning(f"Could not read .env: {exc}")

LOCAL_PORT = 8888
REMOTE_HOST = "127.0.0.1"
REMOTE_PORT = 8888
SSH_HOST = os.environ.get("VPS_HOST", "vmi3134862.contaboserver.net")
SSH_USER = os.environ.get("VPS_USER", "root")
SSH_PASS = os.environ.get("VPS_PASSWORD") or os.environ.get("VPS_PASS", "")


def is_port_in_use(port: int) -> bool:
    """Check if local TCP port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def probe_tunnel_health(port: int, timeout: float = 3.0) -> bool:
    """Perform a quick HTTP ping through the tunnel."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return False
            s.sendall(b"GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:8888\r\nConnection: close\r\n\r\n")
            data = s.recv(256)
            return b"200 OK" in data or b"status" in data or b"HTTP/1." in data
    except Exception:
        return False


def check_existing_instance() -> bool:
    """Check if another tunnel instance is already managing the port."""
    if PID_FILE.exists():
        try:
            pid_str = PID_FILE.read_text(encoding="utf-8").strip()
            if pid_str.isdigit():
                pid = int(pid_str)
                if pid != os.getpid():
                    import psutil
                    if psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        name = proc.name().lower()
                        if "python" in name or "ssh" in name:
                            if probe_tunnel_health(LOCAL_PORT):
                                logger.info(f"Another tunnel instance (PID {pid}) is active and healthy.")
                                return True
        except Exception:
            pass
    return False


def write_pid_file():
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        logger.warning(f"Could not write PID file: {exc}")


def remove_pid_file():
    try:
        if PID_FILE.exists():
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Native OpenSSH Tunnel Worker
# ---------------------------------------------------------------------------

class NativeSshWorker:
    """Manages an OpenSSH (ssh.exe) subprocess with active health recycling."""

    def __init__(self):
        self.process: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._consecutive_probe_failures = 0

    def start(self) -> subprocess.Popen:
        cmd = [
            "ssh.exe",
            "-N",
            "-T",
            "-L", f"{LOCAL_PORT}:{REMOTE_HOST}:{REMOTE_PORT}",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "TCPKeepAlive=yes",
            f"{SSH_USER}@{SSH_HOST}",
        ]
        CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
        logger.info(f"Spawning native OpenSSH tunnel: {SSH_USER}@{SSH_HOST} -> {LOCAL_PORT}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        return self.process

    def stop(self):
        self._stop_event.set()
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

    def health_monitor_loop(self):
        """Monitor tunnel responsiveness; kill process if frozen."""
        time.sleep(3)  # Allow initial handshake
        while not self._stop_event.is_set():
            if self.process and self.process.poll() is None:
                if probe_tunnel_health(LOCAL_PORT, timeout=3.5):
                    self._consecutive_probe_failures = 0
                else:
                    self._consecutive_probe_failures += 1
                    logger.warning(f"Tunnel health probe failed ({self._consecutive_probe_failures}/3)")
                    if self._consecutive_probe_failures >= 3:
                        logger.error("Tunnel unresponsive for 45s. Recycling ssh process...")
                        self._consecutive_probe_failures = 0
                        if self.process:
                            try:
                                self.process.terminate()
                            except Exception:
                                pass
            time.sleep(15)


# ---------------------------------------------------------------------------
# Paramiko Fallback Worker
# ---------------------------------------------------------------------------

def _forward_sockets(source: socket.socket, destination: socket.socket):
    try:
        while True:
            data = source.recv(32768)
            if not data:
                break
            destination.sendall(data)
    except Exception:
        pass
    finally:
        for s in (destination, source):
            try:
                s.close()
            except Exception:
                pass


class ParamikoFallbackTunnel:
    """Pure-Python Paramiko SSH tunnel fallback."""

    def __init__(self):
        self.ssh = None
        self.transport = None
        self.lock = threading.Lock()
        self.running = True

    def connect(self) -> bool:
        import paramiko
        with self.lock:
            if self.transport and self.transport.is_active():
                return True
            if self.ssh:
                try:
                    self.ssh.close()
                except Exception:
                    pass
            logger.info(f"[Paramiko] Connecting tunnel to {SSH_HOST}...")
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(SSH_HOST, username=SSH_USER, password=SSH_PASS, timeout=12)
            self.transport = self.ssh.get_transport()
            if self.transport:
                self.transport.set_keepalive(30)
                logger.info("[Paramiko] SSH Transport connected and 30s keep-alive enabled.")
                return True
            return False

    def is_alive(self) -> bool:
        return self.transport is not None and self.transport.is_active()

    def handle_client(self, client_socket: socket.socket):
        if not self.is_alive():
            try:
                if not self.connect():
                    client_socket.close()
                    return
            except Exception as e:
                logger.warning(f"[Paramiko] Reconnect failed: {e}")
                client_socket.close()
                return

        try:
            remote_channel = self.transport.open_channel(  # type: ignore[union-attr]
                "direct-tcpip",
                (REMOTE_HOST, REMOTE_PORT),
                client_socket.getpeername(),
            )
        except Exception as e:
            logger.warning(f"[Paramiko] open_channel error ({e}), retrying once...")
            try:
                self.connect()
                remote_channel = self.transport.open_channel(  # type: ignore[union-attr]
                    "direct-tcpip",
                    (REMOTE_HOST, REMOTE_PORT),
                    client_socket.getpeername(),
                )
            except Exception as e2:
                logger.warning(f"[Paramiko] Reconnect retry failed: {e2}")
                client_socket.close()
                return

        if remote_channel is None:
            client_socket.close()
            return

        t1 = threading.Thread(target=_forward_sockets, args=(client_socket, remote_channel), daemon=True)
        t2 = threading.Thread(target=_forward_sockets, args=(remote_channel, client_socket), daemon=True)
        t1.start()
        t2.start()

    def run(self):
        while self.running:
            try:
                if self.connect():
                    break
            except Exception as e:
                logger.warning(f"[Paramiko] Initial connection failed: {e}. Retrying in 5s...")
                time.sleep(5)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", LOCAL_PORT))
        server.listen(32)
        logger.info(f"[Paramiko] Listening on http://127.0.0.1:{LOCAL_PORT} -> VPS :{REMOTE_PORT}")

        while self.running:
            try:
                client, _ = server.accept()
                threading.Thread(target=self.handle_client, args=(client,), daemon=True).start()
            except Exception as e:
                logger.warning(f"[Paramiko] Accept error: {e}")
                time.sleep(0.5)


# ---------------------------------------------------------------------------
# Immortal Watchdog Daemon
# ---------------------------------------------------------------------------

class TunnelWatchdog:
    """Supervisory watchdog loop ensuring the tunnel runs 24/7 forever."""

    def __init__(self):
        self.running = True
        self._current_worker: NativeSshWorker | None = None
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        logger.info(f"Received signal {signum}. Terminating watchdog gracefully.")
        self.running = False
        if self._current_worker:
            try:
                self._current_worker.stop()
            except Exception:
                pass

    def run(self):
        if check_existing_instance():
            logger.info("Tunnel daemon already running. Exiting redundant process.")
            return

        write_pid_file()
        logger.info(f"============================================================")
        logger.info(f"Kalshi Swarm Resilient Tunnel Watchdog Started (PID {os.getpid()})")
        logger.info(f"Target: {SSH_USER}@{SSH_HOST} | Forward: 127.0.0.1:{LOCAL_PORT} -> :{REMOTE_PORT}")
        logger.info(f"============================================================")

        # Check if native OpenSSH is present
        has_ssh_cli = False
        try:
            res = subprocess.run(["ssh.exe", "-V"], capture_output=True, text=True)
            has_ssh_cli = res.returncode == 0 or "openssh" in (res.stderr or "").lower()
        except Exception:
            has_ssh_cli = False

        if has_ssh_cli:
            logger.info("Native OpenSSH detected. Using high-performance OpenSSH tunnel worker.")
            self._run_native_watchdog()
        else:
            logger.info("Native OpenSSH not available. Falling back to Paramiko Python tunnel.")
            try:
                ParamikoFallbackTunnel().run()
            finally:
                remove_pid_file()

    def _run_native_watchdog(self):
        backoff = 2
        while self.running:
            try:
                # If local port is already occupied (e.g. leftover zombie), wait for it to clear
                if is_port_in_use(LOCAL_PORT) and not probe_tunnel_health(LOCAL_PORT):
                    logger.warning(f"Port {LOCAL_PORT} occupied by dead socket. Waiting 2s for release...")
                    time.sleep(2)

                worker = NativeSshWorker()
                self._current_worker = worker
                proc = worker.start()

                # Start background health probe
                probe_thread = threading.Thread(target=worker.health_monitor_loop, daemon=True)
                probe_thread.start()

                # Wait for initial binding
                time.sleep(2)
                if probe_tunnel_health(LOCAL_PORT, timeout=3.0):
                    logger.info(f"[TUNNEL ONLINE] http://127.0.0.1:{LOCAL_PORT} -> VPS {REMOTE_HOST}:{REMOTE_PORT}")
                    backoff = 2  # Reset backoff on successful connection

                # Supervise process until exit
                exit_code = proc.wait()
                worker.stop()
                logger.warning(f"SSH process exited with code {exit_code}.")

            except Exception as exc:
                logger.error(f"Watchdog exception: {exc}", exc_info=True)

            if self.running:
                logger.info(f"Reconnecting tunnel in {backoff}s...")
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30)

        remove_pid_file()
        logger.info("Tunnel watchdog stopped.")


if __name__ == "__main__":
    TunnelWatchdog().run()
