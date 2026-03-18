# Learning System User Guide

## Overview

The Kalshi Swarm uses a **two-tier adaptive learning system** that continuously improves trading decisions from real trade outcomes. It requires zero manual intervention during normal operation — everything triggers automatically as trades accumulate.

---

## Architecture at a Glance

```
┌──────────────────────────────────────────────────────────┐
│                    SWARM TIER (every 30 min)             │
│           SwarmMetaAggregator + MetaLearner              │
│    Aggregates all bots → swarm_meta_insights.json        │
└──────────┬────────────────────────────────┬──────────────┘
           │  blends back                   │  blends back
    ┌──────▼──────┐                  ┌──────▼──────┐
    │  Sentinel   │  ...  (x4 bots)  │  Vanguard   │
    │ LearningEng │                  │ LearningEng │
    │  sentinel.db│                  │ vanguard.db │
    └─────────────┘                  └─────────────┘
```

**Level 1 — Per-Bot Local Learning**: Each bot tracks its own trade history, detects performance trends, and recalibrates its feature scoring weights.

**Level 2 — Swarm Meta-Learning**: A coordinator aggregates all four bots' experience every 30 minutes, identifies which market categories are hot/cold across the swarm, and feeds that signal back to every bot.

---

## How It Works

### 1. Trade Logging

Every trade is logged automatically with five feature scores:

| Feature | What It Measures |
|---------|-----------------|
| `edge` | Estimated probability edge over market price |
| `liquidity` | Order book depth and tightness |
| `volume` | Market trading activity level |
| `timing` | How close to resolution or optimal entry |
| `momentum` | Recent price movement direction |

Each score is paired with the market's category, series, confidence level, and a timestamp.

### 2. Outcome Recording

When a trade resolves, the result (win / loss / breakeven) and P&L are written to the bot's SQLite database. The learning engine uses settled outcomes only — open positions are excluded from recalibration.

### 3. Trend Detection (every 50 trades)

After every `rolling_window` trades, the engine computes:

- **Win rate momentum** — recent 50 trades vs prior 50 trades
- **P&L momentum** — recent average P&L vs prior average
- **Hot/cold categories** — which categories beat or lag the bot's overall win rate
- **Confidence calibration** — whether stated confidence matches observed win rate
- **Feature importance** — point-biserial correlation of each score with wins

These combine into a **momentum multiplier** (range 0.7–1.3) that scales confidence thresholds dynamically.

### 4. Weight Recalibration (every 25 trades)

After every `review_interval_trades` trades (minimum 10 settled), the engine recalibrates how much each feature score contributes to the final trade score:

1. Recent trades weighted more heavily via exponential decay (factor ~0.95)
2. New weights blend mean-difference signal (60%) with correlation signal (40%)
3. Weights are normalized to sum to 1.0
4. Each weight is clamped ≥ 0.04 (no feature is ever zeroed out)
5. New weights are saved to the `weight_history` table with a timestamp and trigger reason

### 5. Category Multipliers

Per-category performance multipliers (range 0.7–1.3) adjust trade thresholds based on how a category historically performs vs overall win rate. Strong categories get more aggressive thresholds; weak ones get tighter filters.

### 6. Swarm Aggregation (every 30 minutes)

The `SwarmMetaAggregator` reads the last 2,000 trades from each bot's database and writes `data/swarm_meta_insights.json` containing:

- Swarm-wide win rate and average P&L
- Hot categories (≥8% above swarm average win rate)
- Cold categories (≥8% below swarm average win rate)
- Per-category feature weights and edge statistics
- Best-performing bot per category

Each bot then **blends** its local category multiplier with the swarm-wide signal:

```
blended = w_own × local_multiplier + (1 - w_own) × swarm_multiplier
```

`w_own` scales with local data density — sparse data leans more on the swarm; abundant data trusts the local bot more.

### 7. Prior Knowledge (Bayesian Seeding)

All learning starts from seeded domain priors representing 10 "virtual trades" of domain expertise. As real trades accumulate, observed data gradually takes over. Default priors encode knowledge like:

- Economics markets (Fed, CPI, GDP) have higher expected edge
- Crypto markets have higher variance
- Sports markets have tighter margins

Prior strength is configurable per bot via `prior_knowledge.prior_strength`.

### 8. Meta-Learning Task Memory

A shared `meta_learning.db` stores task-level outcomes keyed by market title and domain. When a new market is evaluated, the system finds similar past markets (via token similarity + domain matching) and returns:

- Recommended confidence offset
- Temperature (exploration vs exploitation)
- Kelly bias suggestion

Predictions activate only after ≥10 prior tasks are stored and when similarity confidence ≥ 0.7.

---

## Data Storage

| File | Contents |
|------|----------|
| `data/{bot_name}.db` | Trade log, weight history, daily summaries, category stats, calibration log |
| `data/meta_learning.db` | Cross-bot task memory, config mutation audit log |
| `data/swarm_meta_insights.json` | Latest aggregated swarm insights (refreshed every 30 min) |
| `data/{bot_name}_risk_state.json` | Per-bot drawdown and loss streak tracking |

Insights older than 2 hours (`insights_max_age_seconds: 7200`) are considered stale and not applied.

---

## Configuration Reference

All settings live in `config/swarm_config.yaml`. Bot-specific overrides go in `config/{bot_name}_config.yaml`.

### Learning Engine

```yaml
learning:
  rolling_window: 50           # Trades before trend detection runs
  review_interval_trades: 25   # Trades between weight recalibrations
  min_trades_for_review: 10    # Minimum settled trades required for recalibration
  learning_rate: 0.05          # Weight adjustment speed (0.0–1.0)
  trend_multiplier_min: 0.7    # Floor for momentum multiplier
  trend_multiplier_max: 1.3    # Ceiling for momentum multiplier
```

### Prior Knowledge

```yaml
prior_knowledge:
  specialist: "general"        # Domain focus: politics / economics / weather / general
  prior_strength: 10           # Virtual trades the prior represents (higher = slower to learn away)
  blend_with_observed: true    # Whether to blend priors with observed data
```

### Meta-Learning (Swarm Tier)

```yaml
meta_learning:
  enabled: true
  aggregation_interval_seconds: 1800   # How often swarm aggregation runs (default 30 min)
  min_confidence_to_apply: 0.7         # Minimum similarity confidence for task predictions
  hot_category_threshold: 0.08         # Win rate delta above swarm avg to flag as hot
  cold_category_threshold: -0.08       # Win rate delta below swarm avg to flag as cold
  min_trades_per_category: 10          # Minimum trades for a category to appear in insights
  insights_max_age_seconds: 7200       # Stale threshold for swarm insights (2 hours)
```

### Meta-Evolver (Safe Parameter Mutation)

```yaml
meta_evolver:
  enabled: true
  mutation_rate: 0.3           # Probability of mutating temperature/threshold per cycle
  immutable_agents:            # Agents protected from mutation
    - "GovernorAgent"
    - "MetaEvolverAgent"
  allow_file_writes: false     # Hard safety constraint — never modified
  allow_self_rewrite: false    # Hard safety constraint — never modified
```

---

## What Happens Automatically

You do not need to do anything for these to occur:

- Trade logging on every analysis cycle
- Outcome recording when orders resolve
- Trend detection every 50 trades
- Weight recalibration every 25 trades (when ≥10 have settled)
- Swarm aggregation every 30 minutes
- Category multiplier blending at the start of each scan cycle
- Task memory updates after each resolved trade

---

## Tuning Guide

### System is too slow to adapt
Decrease `rolling_window` and `review_interval_trades`. Lowering `prior_strength` also lets observed data take over faster.

### System is adapting too aggressively (noisy weights)
Increase `rolling_window` (e.g., 100) and `review_interval_trades` (e.g., 50). Increase `prior_strength` to keep priors more dominant.

### Swarm insights are stale or not applying
Check `data/swarm_meta_insights.json` — if `last_updated` is older than 2 hours, the aggregation job may have stalled. Restart the coordinator or lower `insights_max_age_seconds` temporarily.

### A category is being over- or under-traded
Check category multipliers in the bot's database `category_stats` table. You can adjust `hot_category_threshold` / `cold_category_threshold` to widen or narrow what counts as hot/cold.

### Meta-learning predictions not activating
The system requires ≥10 resolved tasks before making predictions. Check `meta_learning.db` `meta_tasks` table row count and verify `min_confidence_to_apply` is not set too high.

---

## Inspecting Learning State

Query the bot databases directly to see learning progress:

```sql
-- Recent weight recalibrations
SELECT recalibrated_at, trigger_reason, new_weights FROM weight_history
ORDER BY recalibrated_at DESC LIMIT 10;

-- Category performance
SELECT category, wins, losses, avg_pnl FROM category_stats
ORDER BY avg_pnl DESC;

-- Daily summary
SELECT date, trades, wins, losses, gross_pnl FROM daily_summary
ORDER BY date DESC LIMIT 14;

-- Confidence calibration
SELECT confidence_bucket, predicted_rate, observed_rate FROM calibration_log
ORDER BY confidence_bucket;
```

Swarm insights are human-readable JSON:
```bash
cat data/swarm_meta_insights.json
```

---

## Safety Constraints

The meta-evolver only mutates parameters **in memory**. It never writes to config files or modifies bot source code. The `allow_file_writes: false` and `allow_self_rewrite: false` flags are hard-coded safety guards and cannot be overridden via config.
