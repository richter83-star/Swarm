#!/usr/bin/env bash
# WHALE-OS control (Linux / VPS): start | stop | restart | status | calibrate | report | logs
#
# Runs the shadow runner and the HUD in the background, tracked by PID files in
# data/whale_os/run, logging to data/whale_os/logs.  SHADOW MODE ONLY: nothing here
# places orders.  The HUD binds to 127.0.0.1 by default; reach it from your PC with
#   ssh -L 8890:127.0.0.1:8890 root@<vps>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN="$ROOT/data/whale_os/run"
LOGS="$ROOT/data/whale_os/logs"
PY="${PYTHON:-python3}"
mkdir -p "$RUN" "$LOGS"
cd "$ROOT"

declare -A CMD=(
  [shadow]="-m consensus.shadow"
  [hud]="-m consensus.hud"
  [calibration]="-m consensus.calibration --leads 6,24 --max-markets 300"
)

alive() {  # alive NAME -> prints pid if the tracked process is ours and running
  local f="$RUN/$1.pid" pid
  [[ -f "$f" ]] || return 1
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null && tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q "consensus\."; then
    echo "$pid"; return 0
  fi
  rm -f "$f"; return 1
}

start() {
  local name="$1" pid
  if pid="$(alive "$name")"; then echo "$name already running (pid $pid)"; return; fi
  [[ -f "$LOGS/$name.log" ]] && mv -f "$LOGS/$name.log" "$LOGS/$name.log.1"
  # shellcheck disable=SC2086
  nohup "$PY" ${CMD[$name]} >"$LOGS/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "$name started (pid $!)  log $LOGS/$name.log"
}

stop() {
  local name="$1" pid
  if pid="$(alive "$name")"; then kill "$pid"; rm -f "$RUN/$name.pid"; echo "$name stopped (pid $pid)"
  else echo "$name not running"; fi
}

case "${1:-status}" in
  start)     start shadow; start hud ;;
  stop)      stop shadow; stop hud ;;
  restart)   stop shadow; stop hud; sleep 2; start shadow; start hud ;;
  calibrate) start calibration ;;   # runner hot-reloads the table; no restart needed
  report)    "$PY" -m consensus.report ;;
  logs)      for n in shadow hud calibration; do [[ -f "$LOGS/$n.log" ]] && { echo "== $n =="; tail -n 15 "$LOGS/$n.log"; }; done ;;
  status)
    for n in shadow hud calibration; do
      if pid="$(alive "$n")"; then echo "$n RUNNING pid $pid"; elif [[ $n != calibration ]]; then echo "$n stopped"; fi
    done
    echo; "$PY" -m consensus.report ;;
  *) echo "usage: $0 start|stop|restart|status|calibrate|report|logs"; exit 2 ;;
esac
