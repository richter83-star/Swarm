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

## Phase B — Agents (WHALE-OS)
- [x] BOOK — order-book microprice (`consensus/agents/orderbook.py`)
- [x] WHALES — taker flow + large prints (`consensus/agents/flow.py`)
- [x] MODEL — weather via Open-Meteo multi-model (`consensus/agents/weather.py`)
- [x] MODEL — crypto lognormal via Coinbase (`consensus/agents/crypto.py`)
- [x] HISTORY — series price calibration + builder CLI (`consensus/agents/history.py`, `consensus/calibration.py`)
- [x] XVENUE — Polymarket, explicit mappings (`consensus/agents/cross_venue.py`)
- [x] RESEARCH — Gemini + web search, budgeted, price-blind (`consensus/agents/research.py`)
- [ ] Economics / politics domain agents (oracle / sentinel knowledge -> votes)

## Phase C — Shadow mode
- [x] Standalone runner `python -m consensus.shadow` (public data, no keys, no orders)
- [x] Settlement sweep updates ledger P&L and agent reliability
- [x] `config/whale_os.yaml`
- [x] Ledger dedupes to first fire per market; reliability keeps one open forecast per agent+market
- [ ] Run on the VPS for 2-4 weeks; build calibration tables

## Phase D — HUD
- [x] `python -m consensus.hud` — agent tiles, Jev rail + gates, decision tape, consensus matrix, shadow P&L, go/no-go meter; labelled SHADOW
- [x] v2 animated HUD to the WHALE-OS reference: vortex field with source→agent→Jev particle flow, log replay, sources latency, inspector + vote bars, reliability, live book, candles, drift-vs-noise fan, Bayes, matrix, trades_out, tail -f, stat tiles

- [x] Gate funnel (24h by market + last cycle), hold-reason Pareto, and per-market decision strands in the field (spin, zoom, inspect); runner writes per-cycle gate stops to status.json

## Phase E — Live (only after the gate passes)
- [ ] Route Jev `fire` decisions through `risk_manager` sizing + global trade guard in `bot_runner`, demo first

## Go / No-Go gate (before any live capital)
- Resolved fired decisions >= ~400 (detects a ~5c edge)
- Lower bound of 95% CI on mean P&L per contract > 0 after fees
- Zero invalid-P&L events

## Review
- Phase A: 37 new tests; full suite green.
- Phases B-D: 35 more tests; live read-only cycles ran end-to-end (sandbox: 8 markets; your machine: 80 markets, 117s, 0 errors, weather voting on 56). Round-robin discovery added after the first full cycle starved crypto series.
- HUD v2: v1 was rejected (static, far below the reference). Rebuilt; verified with headless screenshots at 1440 and 390 px against the reference crops, zero console errors, frame diff confirms animation. Your machine: 406 tests pass; HUD serving live data (104 markets/cycle, 7 live feeds, book + candles from Kalshi).

## Lessons (this project)
- A visual reference is the spec: render with realistic data and compare side by side before calling a UI done; empty states hide layout gaps.
- Scripted edits: replace exact unique strings; never splice between two index() anchors that may be far apart. Back up a file before scripted multi-region edits.
