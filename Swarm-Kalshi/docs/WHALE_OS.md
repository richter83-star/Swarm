# WHALE-OS (shadow mode)

A consensus swarm for Kalshi. Specialist agents each estimate the probability
that a market resolves YES. **Jev** pools them, discounts agents that read the
same data, applies a deterministic rules veto, and fires only when enough
independent agents agree **and** the edge survives Kalshi's fees.

Shadow mode reads public data only and never places orders. Every decision and
every agent is scored against real settlements, which is the evidence for any
later decision about live capital.

## Agents

| Tile | Agent | Reads | Family |
|---|---|---|---|
| BOOK | `book` | Kalshi order book microprice | microstructure |
| WHALES | `whales` | Kalshi taker flow, large prints | microstructure |
| MODEL | `weather` | Open-Meteo multi-model daily highs (KXHIGH*) | weather_model |
| MODEL | `crypto` | Coinbase spot + realized vol, lognormal (KXBTC*, KXETH*) | crypto_model |
| HISTORY | `history` | Settled-market price calibration per series | history |
| XVENUE | `xvenue` | Polymarket price, explicit mappings only (off by default) | cross_venue |
| RESEARCH | `research` | Gemini + web search, budgeted (off by default) | research |

BOOK and WHALES share a family, so together they count as one vote.

## Run it (from `Swarm-Kalshi/`)

```bash
# 1. One cycle to check everything is wired
python -m consensus.shadow --once

# 2. Optional, recommended: build HISTORY tables (public data, takes a while)
python -m consensus.calibration --series KXHIGHNY,KXHIGHCHI,KXHIGHMIA,KXHIGHAUS,KXHIGHDEN,KXHIGHLAX,KXHIGHPHIL,KXHIGHTDC,KXHIGHTSFO,KXHIGHTSEA,KXHIGHTHOU,KXBTCD,KXETHD --leads 6,24

# 3. Run continuously (every 5 minutes by default)
python -m consensus.shadow

# 4. HUD in another terminal -> http://127.0.0.1:8890
python -m consensus.hud
```

Config: `config/whale_os.yaml`. Data: `data/whale_os/` (ledger.db,
reliability.db, status.json, calibration.json).

## Go / no-go before any live capital

- At least ~400 resolved fired markets (one per market; repeats don't count)
- Lower bound of the 95% CI on mean P&L per contract > 0 after fees
- Agents with negative skill against the market get down-weighted
  automatically; drop any that stay negative

## Known assumptions (verify in shadow)

- Weather station coordinates: check each against the market rules text.
- Weather forecast error by lead day (`base_sigma_f`) is a prior, not fitted.
- Crypto uses Coinbase spot as a proxy; Kalshi settles on a CF Benchmarks index.
- Maker entries assume a fill, so shadow P&L on maker orders is an upper bound.
- Maker fee treatment varies by series; check Kalshi's current fee schedule.
