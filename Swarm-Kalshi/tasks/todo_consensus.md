# Consensus Core (WHALE-OS-style) — Build Plan

Branch: `feat/consensus-core`
Goal: replace the single 0-100 score + one-LLM approve/reject gate with
specialist agents that vote probabilities, a quorum-based decider ("Jev"),
a deterministic rules veto, and a fee-aware edge gate. Prove it in shadow
mode before it ever places an order.

## Phase A — Core (this branch)
- [x] `consensus/schema.py` — `MarketSnapshot`, `AgentVote`, `VetoResult`, `Decision`
- [x] `consensus/fees.py` — Kalshi taker/maker fee math with order-level round-up
- [x] `consensus/vetoes.py` — deterministic veto lane (spread, two-sided book, price band, time to close, liquidity floors, blocked series incl. `KXMVE` combos, ambiguous rules terms)
- [x] `consensus/reliability.py` — per-agent weights from Brier skill vs the market, shrunk toward 1.0
- [x] `consensus/aggregator.py` — log-odds pooling with market anchor, family correlation discount, quorum, net-edge gate, maker-first quote
- [x] `consensus/ledger.py` — decision log + shadow P&L + 95% CI summary
- [x] `tests/test_consensus.py`

## Phase B — Agent adapters (next)
- [ ] Order-book agent (wrap `AnalysisEngine._fair_value`)
- [ ] Research agent (wrap research pipeline `estimated_probability`)
- [ ] Base-rate agent (wrap `prior_knowledge.py`)
- [ ] Domain agents (sentinel / oracle / pulse -> votes, abstain outside domain)
- [ ] Flow agent (momentum, volume spikes, large fills)
- [ ] Cross-venue agent (Polymarket price for matched events)

## Phase C — Shadow mode
- [ ] Recorder in `bot_runner` cycle: build votes, call `ConsensusEngine.decide`, write to `DecisionLedger` — no orders
- [ ] Settlement hook: `ledger.resolve` + `reliability.resolve` on market settlement
- [ ] `consensus:` section in `swarm_config.yaml` (`ConsensusConfig.from_dict`)

## Phase D — HUD
- [ ] Agent tiles, consensus matrix, Jev panel, decision tape, P&L — every number labelled DEMO / SHADOW / LIVE

## Go / No-Go gate (before any live capital)
- Resolved fired decisions >= ~400 (detects a ~5c edge)
- Lower bound of 95% CI on mean P&L per contract > 0 after fees
- Zero invalid-P&L events

## Review
- Phase A: 37 new tests; full suite green.
