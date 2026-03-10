# 30-Day Go/No-Go Plan (Swarm-Kalshi)

Start Date: March 10, 2026  
End Date: April 8, 2026  
Current bankroll reference: ~$14

## Objective

Decide if the system is viable to sell by enforcing strict reliability gates first, then minimum trading-performance gates.

## Non-Negotiable Safety Rules (All 30 Days)

1. Fail-safe behavior only: if guard snapshot is stale/invalid, reject trade.
2. Any new `pnl_invalid` event is a reliability failure.
3. If balance drops below `1200` cents, switch to pause + diagnostics.
4. No config loosening unless weekly gates pass.

## Operating Config Profile (Days 1-30)

Apply to live `config/swarm_config.yaml`:

- `swarm.global_daily_loss_limit_cents: 100`
- `swarm.global_exposure_limit_cents: 300`
- `swarm.enforce_global_trade_guard: true`
- `swarm.trade_guard_max_age_seconds: 45`
- `trading.max_position_pct: 0.005`
- `trading.max_signals_per_cycle: 1`
- `trading.max_open_positions: 1`
- `trading.max_trades_per_day: 2`
- `trading.daily_loss_limit_cents: 50`
- `trading.min_balance_cents: 1200`
- `human_behavior.trade_size_min_multiplier: 0.98`
- `human_behavior.trade_size_max_multiplier: 1.02`
- `learning.trend_multiplier_min: 1.0`
- `learning.trend_multiplier_max: 1.0`
- `central_llm.approval_confidence_floor: 85`
- `central_llm.low_volume_confidence_floor: 90`
- `central_llm.wide_spread_confidence_floor: 90`

## Daily Checklist (Run Every Day)

1. `python3 tools/validate_safety_guards.py`
2. `python3 tools/validate_reliability_matrix.py`
3. `python3 tools/go_no_go_report.py --window-days 7 --phase day7`
4. Verify guard snapshot:
   `python3 - <<'PY'\nimport json; d=json.load(open('data/swarm_trade_guard.json')); print(d.get('valid'), d.get('reason'), d.get('metrics',{}).get('total_balance_cents'))\nPY`
5. If any check fails, pause swarm and do incident review before restart.

## Weekly KPI Gates + Auto-Scale Rules

Use rolling 7-day metrics from `tools/go_no_go_report.py`.

### Green Week

Requirements:
- `pnl_invalid_count == 0`
- `guard_valid_now == True`
- `settled_trades >= 12`
- `win_rate_pct >= 54.0`
- `expectancy_cents >= 1.0`
- `profit_factor >= 1.20`

Action:
- Increase risk one step:
- `max_position_pct += 0.001` (cap at `0.01`)
- `global_exposure_limit_cents += 50` (cap at `500`)

### Yellow Week

Conditions:
- Reliability passes, but performance thresholds not all met.

Action:
- Hold settings unchanged.

### Red Week

Any trigger:
- `pnl_invalid_count > 0`
- `win_rate_pct < 48.0`
- `expectancy_cents < -1.0`
- `profit_factor < 0.90`

Action:
- De-risk one step:
- `max_position_pct -= 0.001` (floor `0.0025`)
- `global_exposure_limit_cents -= 50` (floor `150`)
- Pause if two consecutive red weeks.

## Day-30 Final Decision (April 8, 2026)

### GO (Eligible for Pilot Customer Rollout)

All required:
- `30-day settled_trades >= 50`
- `30-day win_rate_pct >= 53.0`
- `30-day expectancy_cents >= 1.5`
- `30-day profit_factor >= 1.15`
- `30-day pnl_invalid_count == 0`
- No critical runtime incidents (guard bypass, crash loops, unreconciled anomalies)

### HOLD (Continue Internal Validation)

Any condition:
- Reliability clean, but one or more performance gates missed.

### NO-GO (Do Not Sell)

Any condition:
- Repeated reliability failures
- Any unresolved data integrity issue impacting learning
- Bankroll deterioration despite strict limits and gate compliance

## Incident Playbook

When a red condition or failure occurs:

1. Stop swarm.
2. Capture logs:
   `grep -E "Trade rejected by global guard|Trade sizing|P&L invariant failed|crash|exception" -n logs/*.log | tail -n 200`
3. Run:
   `python3 tools/backfill_pnl_invariants.py --apply`
4. Re-run validators.
5. Restart only after clean validator pass.

