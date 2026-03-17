# Kalshi Swarm Health Report Guide

## What is the health report?

The health report is a JSON snapshot of your entire trading swarm's condition,
generated daily at 3 AM by `health_check.py`. It captures 10 diagnostic checks,
auto-fixes safe issues (stale trades, log rotation, drawdown peak resets), and
flags anything needing your attention.

Two files are always written:

| File | Purpose |
|------|---------|
| `data/health_report_latest.json` | Always the most recent report — share this one |
| `data/health_reports/health_report_YYYY-MM-DD.json` | Dated archive, one per day |

---

## How to share with Claude for instant diagnosis

Just paste the full contents of `data/health_report_latest.json` into the chat.
Claude will immediately identify issues, explain what they mean, and suggest fixes.

Example prompt:
> "Here is my Kalshi swarm health report. What should I look at first?"
> [paste JSON here]

---

## Status levels

| Status | Meaning |
|--------|---------|
| `OK` | Everything normal, no action needed |
| `INFO` | Positive signal (e.g. a bot performing above average) |
| `FIXED` | An issue was detected and automatically repaired |
| `WARNING` | Something needs your attention but the swarm is still running |
| `CRITICAL` | Serious issue — act as soon as possible |
| `SKIP` | Check was skipped (usually because psutil is not installed) |

The `overall_status` field at the top of the report reflects the worst status
across all 10 checks. `FIXED` rolls up to `OK` in the overall rating.

---

## The 10 checks explained

### 1. `stale_trades`
Finds pending trades whose expiry date (parsed from the ticker, e.g.
`KXEOWEEK-26MAR14` = March 14 2026) has already passed.
**Auto-fix:** marks them as `expired` with `pnl_cents=0`.

### 2. `duplicate_processes`
Counts running instances of `swarm_daemon.py`, `run_swarm.py`, and each bot
runner. More than one copy of any process is `CRITICAL`.
**No auto-kill** — you must resolve duplicates manually to avoid corrupting state.

### 3. `pnl_anomalies`
Counts trades flagged with `pnl_valid=0` (reconciliation failures caught by the
bots at trade close time). High recent anomaly counts warrant investigation.

### 4. `balance_drawdown`
Reads each bot's `data/{bot}_risk_state.json`.
**Auto-fix:** if the current balance is >15% above the tracked peak (e.g. after
a deposit), the peak is reset so drawdown is calculated from the new baseline.
Flags `WARNING` if any bot's drawdown exceeds 30%.

### 5. `config_consistency`
Validates key `swarm_config.yaml` values:
- `trading.min_confidence_threshold` must equal `learning.min_confidence_threshold`
- `trading.max_position_pct` must be in `[0.01, 0.10]`
- `trading.max_open_positions` must be in `[1, 20]`

### 6. `db_integrity`
Runs SQLite's built-in `PRAGMA integrity_check` on each bot database and flushes
WAL files with `PRAGMA wal_checkpoint`. Corruption is reported as `CRITICAL`.

### 7. `log_size`
Checks `logs/swarm.log` size.
**Auto-fix:** if it exceeds 50 MB, renames it to `swarm.log.old` and starts a
fresh empty file.

### 8. `dead_bots`
Reads each bot's `data/{bot}_status.json`, extracts the `pid` field, and checks
if that process is actually alive (`os.kill(pid, 0)`). A `CRITICAL` is raised
if a bot's status says "running" but the PID is gone.

### 9. `win_rates`
Queries the last 50 closed trades per bot:
- Win rate < 35% → `WARNING` + recommendation to pause
- Win rate > 60% → `INFO` (positive)
- Requires at least 10 trades before flagging

### 10. `memory`
Uses `psutil` to measure RSS memory of all swarm processes.
Any process using more than 500 MB triggers a `WARNING`.
(Skipped if psutil is not installed.)

---

## `bot_summary` fields

```json
{
  "sentinel": {
    "trades": 201,       // total closed trades
    "win_rate": 25.87,   // win rate over last 50 trades (%)
    "pending": 0,        // currently open / pending trades
    "pnl_cents": 632,    // total all-time P&L in cents
    "status": "running"  // from {bot}_status.json
  }
}
```

---

## `actions_taken`

Each string describes something the script fixed automatically, e.g.:
- `"Fixed stale trade id=207 KXEOWEEK-26MAR14 in sentinel.db"`
- `"Auto-reset drawdown peak for oracle to 5309¢"`
- `"Rotated swarm.log (52.3MB) to swarm.log.old"`

---

## `recommendations`

Human-readable suggestions that require your decision, e.g.:
- `"Oracle win rate critically low (16.7%) — consider pausing and reviewing strategy"`
- `"Vanguard 0% win rate on 30 trades — monitor closely"`

---

## How to trigger a health check manually

```bash
# From the project directory
python health_check.py

# Or with the venv
.venv/bin/python health_check.py
```

The report is printed to stdout and saved to `data/health_report_latest.json`.

---

## Setting up the daily cron job

```bash
chmod +x setup_cron.sh
./setup_cron.sh
```

This installs a cron entry that runs at 3 AM daily:

```
0 3 * * * cd /root/Swarm/Swarm-Kalshi && .venv/bin/python health_check.py >> logs/health_check.log 2>&1
```

View the cron log at `logs/health_check.log`.

---

## Telegram notifications

If `telegram.enabled: true` in `swarm_config.yaml` (and `bot_token` / `chat_id`
are set), you will receive a concise summary message after each health check.

You can also set credentials via environment variables:
```bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
```

---

## Where to look when something is CRITICAL

| Critical issue | Where to look |
|----------------|---------------|
| Duplicate processes | `ps aux | grep python` then kill extra PIDs |
| Dead bot | Check `logs/swarm.log` for crash traceback |
| DB corruption | Do NOT delete the DB — contact support and keep a backup |
| 0% win rate | Review recent trades in the dashboard, check market conditions |
