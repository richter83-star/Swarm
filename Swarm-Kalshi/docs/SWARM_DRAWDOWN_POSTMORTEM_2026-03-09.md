# Swarm-Kalshi Drawdown Postmortem

Date: 2026-03-09  
Scope: offline code review + local DB evidence (no live trading changes applied)  
Repository: `D:\kalshi-swarm-new\Swarm-Kalshi`

## Executive Summary

The ~80% drawdown is not a single bug. It is a system-level failure mode driven by:

- Position sizing controls being bypassed after risk sizing.
- Global swarm risk controls existing in code but not enforced in execution.
- P&L reconciliation inconsistencies that can overstate losses and poison learning/autoscale.
- Specialist routing degrading in practice because category metadata is often absent in market payloads.
- LLM gate behavior that can be permissive when rejects are low-confidence.

The current architecture can produce periods of rapid losses even when configs appear conservative.

## Evidence Snapshot (From Local DBs)

- `central_llm_controller.db`
- Decisions: `158`
- Approves: `128`
- Rejects: `30`
- Executed + settled: `7`
- Settled outcomes: `6 losses`, `1 win`

- Per-bot settled trade count:
- `oracle.db`: `0` settled
- `pulse.db`: `0` settled
- `sentinel.db`: `0` settled
- `vanguard.db`: `7` settled (`6L/1W`)

- P&L anomaly examples detected:
- `KXFIGHTMENTION-26MAR07HOLOLI-LIGH`: count `16`, entry `11` -> max theoretical loss `176`, recorded `-815`
- `KXFIGHTMENTION-26MAR07HOLOLI-DANA`: count `8`, entry `24` -> max theoretical loss `192`, recorded `-419`

These anomalies indicate accounting/reconciliation issues beyond pure strategy quality.

## Prioritized Findings

## P0-1: Effective position cap can be exceeded after initial risk sizing

Trigger condition:
- A trade passes `RiskManager.position_size()` and is then scaled by additional multipliers.

Impact:
- Real exposure per trade can exceed the configured cap (`max_position_pct`) by a large factor.
- Drawdown accelerates during losing streaks before daily limits trigger.

Evidence:
- Base sizing: `kalshi_agent/risk_manager.py:313-331`
- Post-risk multipliers:
- trend multiplier: `swarm/bot_runner.py:601-604`
- human multiplier: `swarm/bot_runner.py:604`, `kalshi_agent/human_behavior.py:153-156`
- LLM size multiplier: `swarm/bot_runner.py:629`

## P0-2: Global risk controls are not wired into the order execution path

Trigger condition:
- Bots execute trades directly without checking swarm-level exposure/loss budgets.

Impact:
- `global_daily_loss_limit_cents` and `global_exposure_limit_cents` can be bypassed in live behavior.
- Config says “global limits,” but runtime behavior is effectively per-bot only.

Evidence:
- Direct execution in bot runner: `swarm/bot_runner.py:599-700`
- Coordinator loop does not enforce pre-trade gates: `swarm/swarm_coordinator.py:971-976`
- Balance manager limit functions exist but are unused by order path: `swarm/balance_manager.py:174-187`

## P0-3: Realized P&L reconciliation appears inconsistent (high-risk for learning + autoscale)

Trigger condition:
- Outcome reconciliation path computes/allocates ticker-level settlement and fill P&L.

Impact:
- Reported losses can exceed theoretical max contract loss, distorting:
- risk state
- learning engine training data
- auto-scale allocation decisions
- dashboard trustworthiness

Evidence:
- Settlement math implementation: `swarm/bot_runner.py:877-898`
- Detected anomalies in local `vanguard.db` (see Evidence Snapshot)

## P1-1: Specialist routing degrades because category metadata is often missing

Trigger condition:
- Bot specialization relies heavily on `category_filters`, but Kalshi market payloads frequently do not provide `category`.

Impact:
- Specialist bots may not be operating on true domain boundaries.
- Vanguard catch-all behavior dominates practical execution.
- “Specialist + LLM” value proposition weakens.

Evidence:
- Category-based matching logic: `swarm/bot_runner.py:376-410`
- Scanner sets `category` from raw market dict: `kalshi_agent/market_scanner.py:297`
- Specialist configs rely on `category_filters`:
- `config/sentinel_config.yaml:7`
- `config/oracle_config.yaml:7`
- `config/pulse_config.yaml:7`
- Live API sample check returned no `category` field for tested markets.

## P1-2: LLM gate can be permissive due to fail-soft reject override

Trigger condition:
- LLM returns `reject` with confidence below `min_reject_confidence` while quant confidence is high.

Impact:
- Low-confidence LLM rejections can auto-convert to approvals.
- Central LLM may not serve as a hard risk gate when most quant scores are high.

Evidence:
- Reject override logic: `swarm/central_llm_controller.py:323-335`

## P1-3: Learning loop currently has low signal quality

Trigger condition:
- Very few settled trades relative to pending/open state and short runtime window.

Impact:
- “Learning” is underpowered and noisy.
- Early bad outcomes can dominate adaptation.

Evidence:
- Settled outcomes are sparse in three of four bot DBs.
- Most outcomes observed come from one bot (`vanguard`), reducing cross-specialist signal diversity.

## P2-1: Conflict resolution appears partially implemented in execution path

Trigger condition:
- Comment indicates ticker claim check inside `_execute_trade`, but no claim/release call is present in current runner execution.

Impact:
- Duplicate or conflicting exposure is possible in multi-bot scenarios.
- Could increase correlation risk and compounding losses.

Evidence:
- Comment in loop: `swarm/bot_runner.py:587-589`
- No `claim_ticker`/`release_ticker` call sites in bot runner execution path.

## Root-Cause Chain (Likely)

1. Scanner/analysis emits high-confidence opportunities, including noisy/low-information markets.
2. LLM mostly approves (or fail-soft converts weak rejects).
3. Real order size is amplified post-risk sizing (trend + human + LLM multipliers).
4. Swarm-global caps are not hard-enforced at pre-trade execution.
5. Rapid losses occur in clusters.
6. Reconciliation/accounting anomalies contaminate feedback data.
7. Learning/autoscale reacts to corrupted or sparse signal, not robust edge.

## Fix Plan (P0/P1) With Validation

## Fix Plan for P0-1 (Post-multiplier cap bypass)

Behavior change:
- Introduce a final hard-cap step immediately before order creation:
- `final_notional_cents <= max_position_pct * current_balance_cents`
- Apply cap after all multipliers (trend/human/LLM), not before.
- If capped size becomes `0`, reject trade.

Regression risk:
- Lower trade frequency and slower capital deployment.
- Could reduce upside in high-conviction regimes.

Validation steps:
- Unit test: multipliers cannot increase notional beyond cap.
- Scenario test: worst-case multipliers still capped.
- Runtime log assertion: emit `{base_count, multiplied_count, capped_count}`.

## Fix Plan for P0-2 (Global limits not enforced)

Behavior change:
- Add coordinator-backed pre-trade authorization endpoint or shared gate module.
- Require every order path to check:
- global exposure limit
- global daily loss limit
- bot budget allocation availability
- Block order if any fail; log explicit reason.

Regression risk:
- More coupling between bot runner and coordinator.
- Need robust fallback if coordinator temporarily unavailable.

Validation steps:
- Scenario: exceed global exposure -> order rejected deterministically.
- Scenario: hit global daily loss -> all bots block new entries.
- Static check: no direct order submission path bypasses gate.

## Fix Plan for P0-3 (P&L reconciliation anomalies)

Behavior change:
- Normalize all monetary fields to cents once at ingestion.
- Add invariant checks:
- `abs(loss_pnl) <= count * entry_price + fee_slippage_tolerance`
- If invariant violated, mark outcome as `invalid_pnl`, exclude from learning/autoscale until reconciled.
- Add reconciliation trace fields in DB (source endpoint, raw values, conversion path).

Regression risk:
- Historical metrics may shift after backfill.
- Some old trades may be reclassified/untrusted.

Validation steps:
- Replay historical settlements/fills through pure function tests.
- Property tests for unit conversions and signed P&L bounds.
- Scenario: malformed settlement payload should not update learning with invalid P&L.

## Fix Plan for P1-1 (Specialist routing degradation)

Behavior change:
- Route by stronger signals when `category` missing:
- series prefix map
- event_ticker keyword taxonomy
- title keyword model
- Add `unknown_category_policy`:
- strict specialist mode (drop unknowns)
- or probabilistic assignment with confidence threshold

Regression risk:
- Too-strict routing can starve trade volume.
- Too-loose routing can keep current noise.

Validation steps:
- Offline routing audit on sampled tickers:
- `% routed by category`
- `% routed by series`
- `% routed by title`
- `% unresolved`
- Ensure each specialist receives domain-consistent markets.

## Fix Plan for P1-2 (LLM permissive gate)

Behavior change:
- Add strict mode for central gate:
- `reject` remains reject unless explicit override flag is true.
- Require minimum rationale quality + confidence for approvals in high-vol regimes.
- Optional: confidence floor by market archetype.

Regression risk:
- Trade count drops; may reduce short-term activity.

Validation steps:
- Scenario: weak LLM reject + high quant confidence should remain reject in strict mode.
- Compare approval rate and realized win rate in A/B dry run.

## Recommended Immediate Guardrails (Before Full Refactor)

Apply immediately in config/runtime:

- Lower `max_position_pct` to `0.01` until reconciliation is corrected.
- Set `max_signals_per_cycle` to `1`.
- Narrow human size jitter to `0.85-1.10`.
- Lower trend multiplier ceiling to `1.05`.
- Raise central gate strictness:
- disable reject auto-override behavior
- require higher approval confidence floor
- Turn on “account-scope” open position cap where available.

## Product Readiness Assessment

Current state is not product-safe for external sale as a “set-and-forget” autonomous system.  
The architecture can be made product-safe, but only after:

- hard pre-trade global risk gating,
- corrected P&L accounting invariants,
- reliable specialist routing,
- stricter LLM decision policy,
- and a clean validation matrix pass.

## Validation Matrix to Run After Fixes

- stale status file with old PID
- bot exits before reporting running
- prolonged DB lock during logging/backtest
- repeated 429/5xx storms during scans and order submission
- status-file write/read failure and recovery
- global exposure limit breach attempt
- global daily loss limit breach attempt
- reconciliation with malformed/partial settlement payload
- category-missing routing fallback correctness

## Appendix: Commands Used

- Static grep/code-walk across `swarm/`, `kalshi_agent/`, `config/`, `dashboard/`
- Local SQLite inspection of:
- `data/central_llm_controller.db`
- `data/sentinel.db`
- `data/oracle.db`
- `data/pulse.db`
- `data/vanguard.db`

