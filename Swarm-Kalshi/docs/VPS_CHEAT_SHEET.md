# VPS Cheat Sheet — Kalshi Swarm

**VPS:** `root@vmi3134862`
**Project path:** `/root/Swarm/Swarm-Kalshi`

---

## Connect

```bash
ssh root@vmi3134862
cd ~/Swarm/Swarm-Kalshi
```

---

## Activate Virtual Environment

```bash
source venv/bin/activate
# or if using .venv:
source .venv/bin/activate
```

---

## Start the Swarm

### Quick start (pulls latest code + starts daemon)
```bash
bash start.sh
```

### Manual start
```bash
pkill -f run_swarm_with_ollama_brain.py || true
python3 run_swarm_with_ollama_brain.py --host 127.0.0.1 --port 8081
```

### Via systemd (if configured)
```bash
sudo systemctl start kalshi-swarm
sudo systemctl restart kalshi-swarm
sudo systemctl stop kalshi-swarm
sudo systemctl status kalshi-swarm --no-pager
```

---

## Stop the Swarm

```bash
pkill -f run_swarm_with_ollama_brain.py
# or kill all related processes:
pkill -f swarm_coordinator
pkill -f swarm.bot_runner
```

---

## Check What's Running

```bash
ps -ef | grep -E "swarm_coordinator|swarm.bot_runner|run_swarm" | grep -v grep
```

---

## Pull Latest Code & Restart

```bash
cd ~/Swarm/Swarm-Kalshi
git pull origin main
git log --oneline -n 5
pkill -f run_swarm_with_ollama_brain.py || true
python3 run_swarm_with_ollama_brain.py --host 127.0.0.1 --port 8081
```

---

## Update Dependencies

```bash
source venv/bin/activate
pip install -q -r requirements.txt
```

---

## Health Check

```bash
python3 health_check.py
# View latest report:
cat data/health_report_latest.json
# View logs:
tail -f logs/health_check.log
```

---

## Run Safety Validators (before every restart)

```bash
python3 tools/validate_safety_guards.py
python3 tools/validate_reliability_matrix.py
```

Both must pass before starting the swarm.

---

## View Logs

```bash
# Live log stream
tail -f logs/*.log

# Filter for trade decisions
grep -E "Trade sizing|Trade rejected|hard cap|P&L invariant" logs/*.log | tail -80

# Filter for errors
grep -i "error\|exception\|traceback" logs/*.log | tail -50

# Check trade guard
cat data/swarm_trade_guard.json
```

---

## Inspect Learning Data

```bash
# Check swarm insights (updated every 30 min)
cat data/swarm_meta_insights.json

# Check trade guard snapshot
ls -l data/swarm_trade_guard.json
tail -n 40 data/swarm_trade_guard.json
```

---

## Routing Audit

```bash
python3 tools/audit_routing_coverage.py --sample-size 100 --max-pages 3 --page-limit 500
```

---

## P&L Backfill (quarantine corrupted history)

```bash
# Dry run first
python3 tools/backfill_pnl_invariants.py

# Apply
python3 tools/backfill_pnl_invariants.py --apply
```

---

## Cron Job (daily 3 AM health check)

```bash
# Install
bash setup_cron.sh

# Verify
crontab -l

# Remove
crontab -l | grep -v health_check | crontab -

# Run manually now
cd ~/Swarm/Swarm-Kalshi && .venv/bin/python health_check.py
```

---

## Quick File Checks

```bash
# Confirm venv exists
ls /root/Swarm/Swarm-Kalshi/ | grep -E 'venv|env'

# Check env file
ls -la .env

# Check config
cat config/swarm_config.yaml | grep -E "max_position|daily_loss|exposure_limit"
```

---

## Post-Restart Checklist

```bash
# 1. Check processes are alive
ps -ef | grep -E "swarm_coordinator|swarm.bot_runner" | grep -v grep

# 2. Trade guard is updating
ls -l data/swarm_trade_guard.json

# 3. No crash loops in logs
grep -i "error\|crash\|restart" logs/*.log | tail -20

# 4. Guards and caps are firing correctly
grep -E "Trade rejected|hard cap|global guard" logs/*.log | tail -20
```
