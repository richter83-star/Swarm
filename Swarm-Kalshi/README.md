# Kalshi Bot Swarm v4.0

An autonomous, self-learning multi-bot trading system for the [Kalshi](https://kalshi.com) prediction-markets exchange. Four specialist bots coordinate through a central swarm, share a single account balance, and continuously improve their strategy from real trade outcomes — with no human intervention required.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The 4 Specialist Bots](#the-4-specialist-bots)
3. [Core Modules](#core-modules)
4. [Prerequisites](#prerequisites)
5. [Installation](#installation)
6. [Kalshi API Key Setup](#kalshi-api-key-setup)
7. [Configuration](#configuration)
8. [Environment Variables](#environment-variables)
9. [Running the Swarm](#running-the-swarm)
10. [Docker Deployment](#docker-deployment)
11. [24/7 Daemon Mode](#247-daemon-mode)
12. [Windows Autostart](#windows-autostart)
13. [Dashboard](#dashboard)
14. [Telegram Integration](#telegram-integration)
15. [Per-Resolution Learning](#per-resolution-learning)
16. [Budget Allocation](#budget-allocation)
17. [Safety Features](#safety-features)
18. [Project Structure](#project-structure)
19. [Developer Tools](#developer-tools)
20. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│                  Flask Dashboard  :8080                   │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────┴─────────────────────────────────┐
│                   Swarm Coordinator                       │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Market Router│  │ Balance Mgr  │  │Conflict Resolver│  │
│  └──────────────┘  └──────────────┘  └────────────────┘  │
│                                                           │
│  ┌────────────────────────────────────────────────────┐   │
│  │            Telegram Command Bot (optional)          │   │
│  └────────────────────────────────────────────────────┘   │
└───┬──────────┬──────────┬──────────┬────────────────────┘
    │          │          │          │
┌───┴───┐  ┌──┴────┐  ┌──┴────┐  ┌──┴──────┐
│Sentinel│  │Oracle │  │ Pulse │  │Vanguard │
│Politics│  │Econ   │  │Weather│  │General  │
└───────┘  └───────┘  └───────┘  └─────────┘
    │          │          │          │
 [scan]     [scan]     [scan]     [scan]
 [analyse]  [analyse]  [analyse]  [analyse]
 [execute]  [execute]  [execute]  [execute]
 [learn]    [learn]    [learn]    [learn]
```

Each bot runs its own trading loop (`scan → analyse → execute → reconcile → learn`) in a subprocess managed by the Swarm Coordinator. They share a single Kalshi account; the Balance Manager allocates capital and the Conflict Resolver prevents duplicate trades.

---

## The 4 Specialist Bots

| Bot | Codename | Domain | Focus |
|-----|----------|--------|-------|
| Bot 1 | **Sentinel** | Politics / Elections | Government actions, policy, regulation, geopolitics |
| Bot 2 | **Oracle** | Economics / Finance | CPI, jobs, GDP, Fed rate, inflation, treasury |
| Bot 3 | **Pulse** | Climate / Weather / Science | Temperature records, hurricanes, scientific thresholds |
| Bot 4 | **Vanguard** | Culture / Tech / Crypto | Everything not covered by the other three |

Each bot loads a merged configuration (global `swarm_config.yaml` + its own `{bot}_config.yaml`) and maintains its own SQLite database, log file, and learned weights.

---

## Core Modules

### Trading Core (`kalshi_agent/`)

| Module | Purpose |
|--------|---------|
| `kalshi_client.py` | Authenticated Kalshi API client (RSA private key, `/trade-api/v2`) |
| `market_scanner.py` | Fetches open markets, applies filters, ranks by volume/liquidity/spread |
| `analysis_engine.py` | 5-dimension scoring: edge, liquidity, volume, timing, momentum |
| `risk_manager.py` | Position sizing, daily loss limits, drawdown protection, streak multiplier |
| `learning_engine.py` | SQLite trade log, trend detection, confidence calibration, weight recalibration |
| `llm_advisor.py` | Anthropic Claude second-opinion on high-confidence signals (no SDK, stdlib only) |
| `external_signals.py` | News sentiment, consensus estimates, resolution patterns (cached, optional) |
| `backtester.py` | Pre-calibrates learning DB against settled markets before live trading |
| `prior_knowledge.py` | Seeded Bayesian domain priors that converge to observed data |
| `human_behavior.py` | Human-like timing delays and session patterns |

### Swarm Infrastructure (`swarm/`)

| Module | Purpose |
|--------|---------|
| `swarm_coordinator.py` | Spawns bots as subprocesses, health-monitors, restarts crashed bots |
| `bot_runner.py` | Runs the per-bot trading loop and fires post-resolution learning hooks |
| `market_router.py` | Routes markets to the correct specialist by category, series, keywords |
| `balance_manager.py` | Allocates the shared account balance across bots |
| `conflict_resolver.py` | Prevents two bots from trading the same ticker simultaneously |

### Notifications (`telegram/`)

| Module | Purpose |
|--------|---------|
| `telegram/notifier.py` | Sends trade signals, outcomes, daily summaries, and crash alerts to Telegram |
| `telegram/bot.py` | Long-polling command bot: `/status`, `/pnl`, `/pause`, `/resume`, `/stop` |

---

## Prerequisites

- **Python 3.11** or later
- A **Kalshi account** with API access enabled
- A Kalshi **RSA private key** and the associated **key ID**
- *(Optional)* An **Anthropic API key** for LLM advisory scoring
- *(Optional)* A **Telegram bot token** and chat ID for notifications

---

## Installation

```bash
# 1. Clone or download the project
git clone <repo-url> kalshi-swarm
cd kalshi-swarm

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate       # Linux / macOS
.venv\Scripts\activate          # Windows

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Create required directories (done automatically on first run, but safe to run now)
mkdir -p data logs keys
```

---

## Kalshi API Key Setup

1. Log into [kalshi.com](https://kalshi.com) → **Settings → API**.
2. Click **Create API Key** and choose **RSA** key type.
3. Download the private key file and note the **key ID** (UUID format).
4. Place the private key in the project:

```bash
cp /path/to/downloaded-key.pem keys/kalshi-private.key
```

> **Security:** `keys/` is gitignored. Never commit your private key.

---

## Configuration

### Step 1 — Copy the template

```bash
cp config/swarm_config.yaml.example config/swarm_config.yaml
```

`config/swarm_config.yaml` is gitignored and will never be committed.

### Step 2 — Set your API credentials

```yaml
api:
  key_id: ""                      # Leave blank; set KALSHI_KEY_ID env var instead
  private_key_path: keys/kalshi-private.key
  base_url: https://api.elections.kalshi.com/trade-api/v2
  demo_mode: false                # true = paper trading on Kalshi demo environment
```

> **Recommended:** leave `key_id` blank in the file and export `KALSHI_KEY_ID` as an environment variable. The bot will use the env var if the config value is empty.

### Step 3 — Tune trading parameters

Key parameters under `trading:`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_confidence_threshold` | 65 | Minimum score (0–100) to place a trade |
| `min_liquidity_cents` | 1000 | Skip illiquid markets |
| `min_volume_24h` | 50 | Skip thin markets |
| `min_hours_to_expiry` | 2 | Skip markets resolving too soon |
| `max_hours_to_expiry` | 720 | Skip markets too far in the future |
| `max_position_pct` | 0.05 | Max 5% of bot balance per position |
| `max_open_positions` | 10 | Per-bot open position cap |
| `max_trades_per_day` | 20 | Per-bot daily trade cap |
| `daily_loss_limit_cents` | 3000 | Per-bot $30/day loss halt |
| `max_drawdown_pct` | 0.10 | Halt if balance drops 10% from peak |
| `loss_streak_threshold` | 3 | Trigger reduced sizing after N consecutive losses |
| `loss_streak_size_multiplier` | 0.5 | Reduce position size to 50% during a losing streak |
| `max_signals_per_cycle` | 3 | Max trades to execute per scan cycle |
| `stale_trade_hours` | 48 | Force-resolve trades older than this |

### Step 4 — (Optional) LLM Advisor

The LLM advisor calls Anthropic Claude to provide a second opinion on high-confidence signals. It blends the result with the quantitative score (default: 20% LLM, 80% quant).

```yaml
llm_advisor:
  enabled: true
  api_key: ""                     # Leave blank; set ANTHROPIC_API_KEY env var
  model: claude-haiku-4-5-20251001
  pre_screen_threshold: 60        # Only call LLM if quant confidence >= this
  llm_weight: 0.20
  max_calls_per_cycle: 5          # Hard cap to control API costs
```

If `ANTHROPIC_API_KEY` is not set and no key is provided in config, the LLM advisor disables itself gracefully — all other functionality continues normally.

### Step 5 — (Optional) Telegram

See [Telegram Integration](#telegram-integration) below.

---

## Environment Variables

All secrets can (and should) be passed via environment variables rather than stored in config files.

| Variable | Required | Description |
|----------|----------|-------------|
| `KALSHI_KEY_ID` | **Yes** (if not in config) | Kalshi API key ID (UUID) |
| `ANTHROPIC_API_KEY` | No | Enables LLM advisor |
| `TELEGRAM_BOT_TOKEN` | No | Enables Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Target channel/chat for notifications |
| `DASHBOARD_USER` | No | Basic-auth username for the web dashboard |
| `DASHBOARD_PASS` | No | Basic-auth password for the web dashboard |

Example `.env` file (load with `export $(cat .env | xargs)` or your deployment tool):

```bash
KALSHI_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=-100xxxxxxxxxx
DASHBOARD_USER=admin
DASHBOARD_PASS=a-very-strong-password
```

---

## Running the Swarm

### Full swarm (recommended)

Starts the coordinator, all 4 bots, and the web dashboard:

```bash
python run_swarm.py
```

### Command-line options

```
python run_swarm.py [OPTIONS]

Options:
  --bot {sentinel,oracle,pulse,vanguard}   Run a single bot (no coordinator)
  --dashboard-only                         Dashboard only, no trading
  --no-dashboard                           Swarm without the web dashboard
  --host HOST                              Dashboard host (default: 0.0.0.0)
  --port PORT                              Dashboard port (default: 8080)
  --log-level {DEBUG,INFO,WARNING,ERROR}   Logging verbosity (default: INFO)
```

**Examples:**

```bash
# Run only the economics bot
python run_swarm.py --bot oracle

# Run swarm on a non-standard port, debug logging
python run_swarm.py --port 9090 --log-level DEBUG

# Dashboard only (review historical performance)
python run_swarm.py --dashboard-only

# Headless (no dashboard, e.g. on a server)
python run_swarm.py --no-dashboard
```

### First-run behaviour

On the first run with an empty database, each bot automatically runs the **backtester** against recently settled Kalshi markets to pre-populate the learning engine with calibrated outcomes. This usually takes 30–60 seconds per bot before live trading begins.

---

## Docker Deployment

### Single container (default)

```bash
# Build the image
docker build -t kalshi-swarm .

# Run with environment variables
docker run -d \
  --name kalshi-swarm \
  -p 8080:8080 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/keys:/app/keys \
  -v $(pwd)/config:/app/config \
  -e KALSHI_KEY_ID=your-key-id-here \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e TELEGRAM_BOT_TOKEN=... \
  -e TELEGRAM_CHAT_ID=... \
  kalshi-swarm
```

### Docker Compose

```bash
# Start (detached)
docker-compose up -d

# Follow logs
docker-compose logs -f

# Stop
docker-compose down
```

Edit `docker-compose.yml` to set environment variables or mount additional volumes.

> **Persistent data:** Always mount `data/`, `logs/`, and `keys/` as volumes so trade history and learned weights survive container restarts.

---

## 24/7 Daemon Mode

`swarm_daemon.py` is a watchdog that keeps the swarm running indefinitely with automatic restarts:

- **Exponential back-off** — waits 30 s, 60 s, 120 s … up to 10 minutes between restarts.
- **Back-off reset** — if the process runs healthily for ≥ 30 minutes, the back-off resets to the base interval.
- **Clean exit** — exit code 0 from the swarm stops the daemon (intentional shutdown).
- **Signal passthrough** — `SIGINT`/`SIGTERM` are forwarded to the child process for graceful shutdown.
- **Rotating log** — `logs/daemon.log` (10 MB × 5 files).

```bash
# Run interactively
python swarm_daemon.py

# Run detached (Linux/macOS — nohup or systemd)
nohup python swarm_daemon.py > /dev/null 2>&1 &

# Run detached (Windows — no console window)
pythonw swarm_daemon.py
```

---

## Windows Autostart

To register the swarm as a Windows Task Scheduler task that starts at boot:

```powershell
# Run PowerShell as Administrator
.\setup_autostart.ps1
```

This creates a task called **KalshiSwarm** that runs `swarm_daemon.py` at system startup using the Python interpreter in your virtual environment (or system Python if no venv is found).

To remove the task:

```powershell
Unregister-ScheduledTask -TaskName "KalshiSwarm" -Confirm:$false
```

---

## Dashboard

Access the web dashboard at **http://localhost:8080** after starting the swarm.

### Enabling authentication

Set credentials via environment variables (recommended) or in `swarm_config.yaml`:

```bash
export DASHBOARD_USER=admin
export DASHBOARD_PASS=your-secure-password
```

```yaml
dashboard:
  auth:
    enabled: true
    username: admin
    password: ""       # use DASHBOARD_PASS env var
```

### Dashboard views

| View | Description |
|------|-------------|
| **Overview** | All 4 bots at a glance — global metrics, cumulative P&L chart, win-rate comparison |
| **Sentinel** | Detailed performance, trades, calibration history, and weight history |
| **Oracle** | Same detail view for the economics bot |
| **Pulse** | Same detail view for the weather/science bot |
| **Vanguard** | Same detail view for the general/catch-all bot |
| **Analytics** | Cross-bot P&L comparison, trade distribution, daily P&L heatmap, ROI ranking |
| **Activity** | Real-time activity feed — bot starts, stops, trades, errors |
| **Controls** | Start, stop, pause, and resume individual bots; adjust budget allocations |

The dashboard auto-refreshes every 15 seconds (configurable with `dashboard.refresh_interval_seconds`).

---

## Telegram Integration

The swarm can send real-time notifications to a Telegram channel and accept control commands from authorised users.

### Creating your Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts to create a bot.
3. Copy the **bot token** (format: `1234567890:ABCdef...`).
4. To get your **chat ID**:
   - Add the bot to your channel/group, or message it directly.
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after sending a message.
   - Copy the `id` value from the `chat` object in the response.

### Enabling Telegram

Set credentials via environment variables:

```bash
export TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
export TELEGRAM_CHAT_ID=-100xxxxxxxxxx
```

Or in `config/swarm_config.yaml`:

```yaml
telegram:
  enabled: true
  bot_token: ""                   # use TELEGRAM_BOT_TOKEN env var
  chat_id: ""                     # use TELEGRAM_CHAT_ID env var
  notify_signals: true            # high-confidence trade signals
  notify_trades: true             # executed trade confirmations
  notify_outcomes: true           # trade resolution + P&L
  notify_crashes: true            # bot crash/restart alerts
  notify_daily_summary: true      # end-of-session summary
  signal_confidence_threshold: 70 # only notify for signals >= this confidence
  commands_enabled: true
  allowed_user_ids: [123456789]   # Telegram user IDs allowed to send commands
  poll_interval_seconds: 2
```

### Notification types

| Event | Message example |
|-------|----------------|
| Signal | `[Sentinel] HIGH CONFIDENCE signal: KXPOL-2024NOV05-T on YES @ 73¢ (conf=78.4)` |
| Trade executed | `[Oracle] Bought 12 × KXECON-CPIDEC-B YES @ 34¢` |
| Trade resolved | `[Pulse] KXWX-2024DEC-TEMP resolved WIN +$4.20 (bot P&L today: +$12.50)` |
| Crash | `[Vanguard] crashed (exit code -11). Restarting...` |
| Daily summary | `[Swarm] Day end: 7W/3L, P&L +$18.40, best bot: Oracle` |

### Control commands

Send these commands to the bot in Telegram (only `allowed_user_ids` are permitted):

| Command | Action |
|---------|--------|
| `/status` | Show all bot states, open positions, today's P&L |
| `/pnl` | Per-bot profit/loss summary for today |
| `/pause [bot]` | Pause a specific bot or all bots (`/pause oracle`) |
| `/resume [bot]` | Resume a paused bot or all bots |
| `/stop confirm` | Gracefully shut down the entire swarm (requires "confirm" to prevent accidents) |
| `/help` | List available commands |

---

## Per-Resolution Learning

The learning engine updates automatically after **every single trade resolution** — no batching, no human trigger.

### How it works

1. A trade is placed and tracked in the bot's SQLite database.
2. When the trade resolves (WIN / LOSS / EXPIRED), `_on_trade_resolved()` is called immediately.
3. **`compute_trend()`** runs instantly — updates the rolling win rate, momentum multiplier, and calibration metrics in memory.
4. If enough trades have accumulated (`min_trades_for_review`, default: 10), **`review_and_recalibrate()`** runs:
   - Analyses feature importance across the rolling window (default: 50 trades).
   - Applies decay-weighted recalibration to the 5 scoring weights (edge, liquidity, volume, timing, momentum).
   - Saves the new weights to the database.
   - Updates the live `AnalysisEngine` so the **next scan cycle uses the new weights**.

### Learning configuration

```yaml
learning:
  rolling_window: 50              # trades included in trend analysis
  min_trades_for_review: 10       # trades required before recalibration triggers
  learning_rate: 0.05             # how aggressively weights shift per review
  decay_factor: 0.95              # older trades weighted less than recent ones
  min_confidence_threshold: 65    # minimum confidence used for edge calculation
```

### What gets learned

| Metric | Effect |
|--------|--------|
| Win rate | Scales the confidence multiplier for future signals |
| Feature importance | Shifts scoring weights toward the dimensions that predicted wins |
| Momentum multiplier | Amplifies sizing during win streaks, reduces during loss streaks |
| Calibration error | Tracks how well the bot's confidence scores predict actual win rates |

---

## Budget Allocation

The default allocation splits the account balance across bots:

| Bot | Default % | Rationale |
|-----|-----------|-----------|
| Sentinel | 25% | Moderate — political markets are liquid but volatile |
| Oracle | 30% | Highest — economic indicators have the most exploitable edges |
| Pulse | 20% | Lower — weather markets are less liquid |
| Vanguard | 25% | Moderate — broad coverage, adaptive |

Allocations are set in each bot's config file (`config/{bot}_config.yaml`) and can be changed at runtime via the dashboard Controls view.

Global limits (configured in `swarm_config.yaml`) act as hard stops regardless of per-bot settings:

- **`global_daily_loss_limit_cents`** (default: $150) — halts ALL bots if hit.
- **`global_exposure_limit_cents`** (default: $500) — maximum total capital at risk across all bots.

---

## Safety Features

The swarm has multiple independent layers of protection:

| Layer | What it does |
|-------|-------------|
| **Min balance check** | Stops trading if balance drops below `min_balance_cents` ($5 default) |
| **Per-bot daily loss limit** | Pauses a bot if it loses more than `daily_loss_limit_cents` in one day |
| **Global daily loss limit** | Halts all bots if the swarm loses more than `global_daily_loss_limit_cents` |
| **Global exposure cap** | Prevents the total at-risk from exceeding `global_exposure_limit_cents` |
| **Max drawdown** | Pauses a bot if balance drops `max_drawdown_pct` % from its peak |
| **Loss streak multiplier** | Reduces position size by `loss_streak_size_multiplier` after N consecutive losses |
| **Max open positions** | Caps concurrent positions per bot at `max_open_positions` |
| **Max trades per day** | Caps daily trades per bot at `max_trades_per_day` |
| **Conflict resolver** | Prevents two bots from trading the same market ticker |
| **Stale trade cleanup** | Force-resolves or exits positions older than `stale_trade_hours` |
| **Exit loss threshold** | Exits a position early if unrealized loss exceeds `exit_loss_threshold_cents` |
| **Parlay exclusion** | Blocks `KXMVECROSS*` series (parlay contracts) by default |
| **Backtester warm-up** | Pre-calibrates learning engine before risking real money |
| **Auto-restart** | Coordinator restarts crashed bots (up to `max_restart_attempts`, with cooldown) |
| **Human-like timing** | Randomised delays between API calls to avoid rate-limit patterns |

---

## Project Structure

```
kalshi-swarm/
├── config/
│   ├── swarm_config.yaml.example   # Committed template — copy to swarm_config.yaml
│   ├── swarm_config.yaml           # Your credentials — gitignored, never committed
│   ├── sentinel_config.yaml        # Politics bot overrides
│   ├── oracle_config.yaml          # Economics bot overrides
│   ├── pulse_config.yaml           # Weather bot overrides
│   └── vanguard_config.yaml        # General bot overrides
│
├── kalshi_agent/                   # Core bot code (shared by all bots)
│   ├── agent.py                    # Main single-bot agent loop
│   ├── kalshi_client.py            # Kalshi API client
│   ├── market_scanner.py           # Market scanning and filtering
│   ├── analysis_engine.py          # 5-dimension trade scoring
│   ├── risk_manager.py             # Position sizing and risk gates
│   ├── learning_engine.py          # Adaptive learning + recalibration
│   ├── llm_advisor.py              # Anthropic Claude second-opinion
│   ├── external_signals.py         # External data signals
│   ├── backtester.py               # Pre-calibration backtester
│   ├── prior_knowledge.py          # Bayesian domain priors
│   ├── human_behavior.py           # Human-like timing patterns
│   └── dashboard.py                # Text-mode diagnostic reports
│
├── swarm/
│   ├── swarm_coordinator.py        # Central coordinator (spawns, monitors bots)
│   ├── bot_runner.py               # Per-bot trading loop + learning hooks
│   ├── market_router.py            # Routes markets to the right specialist
│   ├── balance_manager.py          # Shared balance allocation
│   └── conflict_resolver.py        # Duplicate-trade prevention
│
├── telegram/
│   ├── notifier.py                 # Outbound Telegram notifications
│   └── bot.py                      # Inbound command bot (long-polling)
│
├── dashboard/
│   ├── dashboard_web.py            # Flask backend
│   └── templates/
│       └── dashboard.html          # Full SPA dashboard UI
│
├── tools/                          # Development and diagnostic utilities
│   ├── verify_api_connection.py    # Test Kalshi API auth
│   ├── debug_bot_balance.py        # Show raw balance API response
│   ├── debug_markets.py            # Dump live market data
│   ├── debug_filter.py             # Test market filter logic
│   ├── find_liquid.py              # Find most liquid markets
│   ├── fix_filter.py               # One-off filter debug helper
│   ├── test_filter.py              # Unit-style filter tests
│   └── test_filter_logic.py        # Filter logic edge case tests
│
├── data/                           # SQLite databases (gitignored)
├── logs/                           # Log files (gitignored)
├── keys/                           # API key files (gitignored)
│
├── run_swarm.py                    # Main entry point
├── swarm_daemon.py                 # 24/7 watchdog daemon
├── setup_autostart.ps1             # Windows Task Scheduler registration
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Developer Tools

The `tools/` directory contains diagnostic scripts for development and debugging:

| Script | Purpose |
|--------|---------|
| `verify_api_connection.py` | Verifies Kalshi API authentication and prints account balance |
| `debug_bot_balance.py` | Shows raw balance API response structure |
| `debug_markets.py` | Dumps live market data to stdout |
| `debug_filter.py` | Tests the market filter with your current config |
| `find_liquid.py` | Lists the most liquid open markets |
| `test_filter.py` | Runs filter acceptance tests against live data |
| `test_filter_logic.py` | Tests filter edge cases with synthetic data |

Run any tool from the project root:

```bash
python tools/verify_api_connection.py
```

---

## Troubleshooting

### Bot won't start — "ValueError: KALSHI_KEY_ID not set"

The bot requires your Kalshi key ID. Provide it via environment variable:

```bash
export KALSHI_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
python run_swarm.py
```

Or add it to `config/swarm_config.yaml` under `api.key_id`.

### "Permission denied" reading the private key

Check file permissions:

```bash
chmod 600 keys/kalshi-private.key
```

### No markets passing filters

Lower `min_liquidity_cents` or `min_volume_24h` in `swarm_config.yaml`, or run `tools/debug_filter.py` to see how many markets each filter removes.

### LLM advisor disabled

If you see `LLMAdvisor: no API key found` in logs, the LLM advisor has been skipped. This is non-fatal — quantitative scoring continues without it. To enable it, set `ANTHROPIC_API_KEY`.

### Dashboard shows no data

If running `--dashboard-only`, ensure the `data/` directory contains the bot SQLite databases from a previous run. The dashboard reads directly from the databases.

### Telegram commands not responding

1. Confirm `telegram.commands_enabled: true` in config.
2. Confirm your Telegram user ID is in `telegram.allowed_user_ids`.
3. Check logs for `TelegramCommandBot` entries to see if the polling thread started.

### Docker: data not persisting between restarts

Ensure your `docker-compose.yml` mounts volumes:

```yaml
volumes:
  - ./data:/app/data
  - ./logs:/app/logs
  - ./keys:/app/keys
  - ./config:/app/config
```

---

## License

This project is for personal use. The Kalshi API is subject to [Kalshi's Terms of Service](https://kalshi.com/legal/terms-of-service). Automated trading is permitted under their API terms; review the current terms before deploying.
