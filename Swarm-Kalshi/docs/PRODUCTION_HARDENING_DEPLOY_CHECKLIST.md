# Production Hardening Deploy Checklist

Date: 2026-03-09  
Scope: deploy and verify the P0/P1 safety hardening set on VPS

## 1) Pull Latest Code

```bash
cd ~/Swarm/Swarm-Kalshi
git pull origin main
git log --oneline -n 5
```

Expected commits include:
- `1cc2aa4` historical P&L backfill tool
- `72372be` reliability matrix validator
- `d2c1171` strict LLM gate hardening
- `34c39e1` routing audit + routing bug fix
- `2264dd2` core P0/P1 safety hardening

## 2) Ensure Runtime Config Is Conservative

Verify your live `config/swarm_config.yaml` includes these values:

- `trading.max_position_pct: 0.01`
- `trading.max_signals_per_cycle: 1`
- `human_behavior.trade_size_min_multiplier: 0.95`
- `human_behavior.trade_size_max_multiplier: 1.05`
- `swarm.global_daily_loss_limit_cents: 500` (or your chosen strict limit)
- `swarm.global_exposure_limit_cents: 1000` (or your chosen strict cap)
- `swarm.enforce_global_trade_guard: true`
- `swarm.trade_guard_max_age_seconds: 45`
- `central_llm.approval_confidence_floor: 75`
- `central_llm.low_volume_confidence_floor: 85`
- `central_llm.wide_spread_confidence_floor: 80`
- `learning.trend_multiplier_min: 1.0`
- `learning.trend_multiplier_max: 1.05`

If missing, merge from:
- [swarm_config.yaml.example](/D:/kalshi-swarm-new/Swarm-Kalshi/config/swarm_config.yaml.example)

## 3) Run Offline Safety Validators

```bash
python3 tools/validate_safety_guards.py
python3 tools/validate_reliability_matrix.py
```

Both must pass before restart.

## 4) Quarantine Historical P&L Anomalies

Dry-run first:

```bash
python3 tools/backfill_pnl_invariants.py
```

Apply quarantine:

```bash
python3 tools/backfill_pnl_invariants.py --apply
```

This marks impossible historical outcomes as `pnl_invalid` so learning/autoscale ignores corrupted rows.

## 5) Optional Routing Coverage Audit

```bash
python3 tools/audit_routing_coverage.py --sample-size 100 --max-pages 3 --page-limit 500
```

Review:
- category source distribution
- bot routing distribution
- unresolved/default-bot ratio

If unresolved ratio is high, tune:
- `config/routing_config.yaml`

## 6) Restart Swarm

If running manually:

```bash
pkill -f run_swarm_with_ollama_brain.py || true
python3 run_swarm_with_ollama_brain.py --host 127.0.0.1 --port 8081
```

If using systemd:

```bash
sudo systemctl restart kalshi-swarm
sudo systemctl status kalshi-swarm --no-pager
```

## 7) Post-Restart Verification (Critical)

Check these artifacts:

- Trade guard snapshot exists and updates:
```bash
ls -l data/swarm_trade_guard.json
tail -n 40 data/swarm_trade_guard.json
```

- Guard and cap logs appear:
```bash
grep -E \"Trade sizing|Trade rejected by global guard|hard cap|P&L invariant failed\" -n logs/*.log | tail -n 80
```

- No immediate crashes/restart loops:
```bash
ps -ef | grep -E \"swarm_coordinator|swarm.bot_runner\" | grep -v grep
```

## 8) 24-Hour Dry-Run Gate Before Customer Rollout

Pass criteria:
- No `pnl_invalid` generated from new trades.
- No trade executed without valid guard snapshot.
- No capped size exceeding `max_position_pct`.
- LLM rejects remain rejects.
- Daily loss/exposure limits block correctly when thresholds are hit.

If any fail:
- stop rollout
- keep limits tight
- inspect logs and DB traces before re-enable.
