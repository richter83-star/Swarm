# Kalshi Bot Swarm v3.0.0

A multi-bot autonomous trading system for the [Kalshi](https://kalshi.com) prediction-markets exchange. Four specialist bots coordinate through a central swarm coordinator, sharing a single account while each focusing on its domain of expertise.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                   Command Center (Flask)                 │
│              http://localhost:8080                        │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────┴─────────────────────────────────┐
│                  Swarm Coordinator                        │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐  │
│  │ Market Router │ │Balance Mgr   │ │Conflict Resolver │  │
│  └──────────────┘ └──────────────┘ └──────────────────┘  │
└───┬──────────┬──────────┬──────────┬─────────────────────┘
    │          │          │          │
┌───┴───┐ ┌───┴───┐ ┌───┴───┐ ┌───┴───┐
│Sentinel│ │Oracle │ │ Pulse │ │Vanguard│
│Politics│ │Econ   │ │Weather│ │General │
└───────┘ └───────┘ └───────┘ └───────┘
```

## The 4 Specialist Bots

| Bot | Codename | Domain | Focus |
|-----|----------|--------|-------|
| Bot 1 | **Sentinel** | Politics / Elections | Government actions, policy, regulation, geopolitics |
| Bot 2 | **Oracle** | Economics / Finance | CPI, jobs, GDP, Fed rate, inflation, treasury |
| Bot 3 | **Pulse** | Climate / Weather / Science | Temperature, hurricanes, data-driven thresholds |
| Bot 4 | **Vanguard** | Culture / Tech / Crypto | Everything not covered by the other three |

Each bot is a **full instance** of the v2 trading agent enhanced with three v3 modules.

## v3 Enhancements

Each bot includes these modules on top of the v2 core:

| Module | Purpose |
|--------|---------|
| `backtester.py` | Runs scoring logic against settled Kalshi markets to pre-load the learning engine with calibrated trade outcomes before risking real money. Auto-runs on first launch when the database is empty. |
| `external_signals.py` | Fetches external signals (news sentiment, consensus estimates, resolution patterns) to improve fair-value estimates. Cached per scan cycle, degrades gracefully when sources are unavailable. |
| `prior_knowledge.py` | Seeded Bayesian domain knowledge (category priors, market-type resolution rates, scoring weights, series-level edge priors). Blended with observed data as it accumulates. |

## Swarm Infrastructure

| Component | File | Purpose |
|-----------|------|---------|
| Coordinator | `swarm/swarm_coordinator.py` | Spawns, monitors, and restarts bots; enforces global limits |
| Bot Runner | `swarm/bot_runner.py` | Runs individual bot instances with merged config |
| Market Router | `swarm/market_router.py` | Routes markets to the correct specialist by category, series, and keywords |
| Balance Manager | `swarm/balance_manager.py` | Allocates the shared account balance across bots |
| Conflict Resolver | `swarm/conflict_resolver.py` | Prevents two bots from trading the same ticker |

## Quick Start

### Prerequisites

- Python 3.11 or later
- A Kalshi API key (RSA private key + key ID)

### Installation

```bash
# Clone or unzip the project
cd kalshi-swarm

# Install dependencies
pip install -r requirements.txt

# Place your API key
cp /path/to/your/kalshi-private.key keys/kalshi-private.key
```

### Configuration

Edit `config/swarm_config.yaml` and set your API credentials:

```yaml
api:
  key_id: YOUR_API_KEY_ID_HERE
  private_key_path: keys/kalshi-private.key
  base_url: https://api.elections.kalshi.com/trade-api/v2
  demo_mode: false
```

### Running

```bash
# Full swarm (all 4 bots + coordinator + dashboard)
python run_swarm.py

# Dashboard only (view past data without trading)
python run_swarm.py --dashboard-only

# Single bot
python run_swarm.py --bot sentinel

# Custom dashboard port
python run_swarm.py --port 9090

# Without dashboard
python run_swarm.py --no-dashboard
```

### Docker Deployment

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Command Center Dashboard

Access at **http://localhost:8080** after starting the swarm.

The dashboard provides eight views:

1. **Overview** -- All 4 bots at a glance with global metrics, cumulative P&L chart, and win-rate comparison.
2. **Sentinel** -- Detailed performance, trades, calibration, and weight history for the politics bot.
3. **Oracle** -- Same detail view for the economics bot.
4. **Pulse** -- Same detail view for the weather/science bot.
5. **Vanguard** -- Same detail view for the general/catch-all bot.
6. **Analytics** -- Cross-bot P&L comparison, trade distribution, daily P&L heatmap, and ROI ranking.
7. **Activity** -- Real-time activity feed showing bot starts, stops, trades, and errors.
8. **Controls** -- Start, stop, pause, and resume individual bots; adjust budget allocations.

The dashboard auto-refreshes every 15 seconds (configurable).

## Project Structure

```
kalshi-swarm/
├── config/
│   ├── swarm_config.yaml        # Global settings
│   ├── sentinel_config.yaml     # Politics bot overrides
│   ├── oracle_config.yaml       # Economics bot overrides
│   ├── pulse_config.yaml        # Weather bot overrides
│   └── vanguard_config.yaml     # General bot overrides
├── kalshi_agent/                 # Core bot code (shared by all)
│   ├── __init__.py
│   ├── agent.py                 # Main agent loop (v2)
│   ├── kalshi_client.py         # Kalshi API client (v2)
│   ├── market_scanner.py        # Market scanning (v2)
│   ├── analysis_engine.py       # Trade scoring (v2)
│   ├── human_behavior.py        # Human-like patterns (v2)
│   ├── risk_manager.py          # Risk management (v2)
│   ├── learning_engine.py       # Adaptive learning (v2)
│   ├── dashboard.py             # Text reports (v2)
│   ├── backtester.py            # Backtesting engine (v3)
│   ├── external_signals.py      # External data signals (v3)
│   └── prior_knowledge.py       # Bayesian priors (v3)
├── swarm/
│   ├── __init__.py
│   ├── swarm_coordinator.py     # Central coordinator
│   ├── bot_runner.py            # Individual bot runner
│   ├── market_router.py         # Market-to-bot routing
│   ├── balance_manager.py       # Budget allocation
│   └── conflict_resolver.py     # Duplicate prevention
├── dashboard/
│   ├── __init__.py
│   ├── dashboard_web.py         # Flask backend
│   └── templates/
│       └── dashboard.html       # Full UI
├── data/                        # Per-bot SQLite databases
├── logs/                        # Per-bot log files
├── keys/                        # API key files
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run_swarm.py                 # Main entry point
└── README.md
```

## Budget Allocation

The default allocation splits the account balance across bots:

| Bot | Default % | Rationale |
|-----|-----------|-----------|
| Sentinel | 25% | Moderate -- political markets are liquid but volatile |
| Oracle | 30% | Highest -- economic indicators have the most exploitable edges |
| Pulse | 20% | Lower -- weather markets are less liquid |
| Vanguard | 25% | Moderate -- broad coverage, adaptive |

Allocations can be changed at runtime through the dashboard or by editing `swarm_config.yaml`.

## Safety Features

The swarm includes multiple layers of risk protection:

- **Per-bot daily loss limits** prevent any single bot from losing too much.
- **Global daily loss limit** (default: $150) halts all trading if the swarm is losing.
- **Global exposure limit** (default: $500) caps total capital at risk.
- **Conflict resolver** ensures no two bots trade the same market.
- **Human-like behavior** staggers API calls and varies trade timing.
- **Auto-restart** recovers crashed bots (up to 3 attempts with cooldown).
- **Backtester** pre-calibrates the learning engine before risking real money.
- **Bayesian priors** provide sensible starting points that converge to observed data.

## Platform Compatibility

- **Windows**: Fully supported for local development. Use `python run_swarm.py` directly.
- **Linux**: Fully supported. Use Docker Compose for production deployment.
- **macOS**: Fully supported for local development.

## License

This project is for personal use. The Kalshi API is subject to Kalshi's Terms of Service.
