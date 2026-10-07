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

One control script per platform runs the shadow runner and the HUD in the
background (PID files in `data/whale_os/run`, logs in `data/whale_os/logs`).

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 start      # runner + HUD
powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 status     # processes + report
powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 calibrate  # rebuild HISTORY table (~25 min)
powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 stop
```

```bash
# Linux / VPS (HUD stays on 127.0.0.1; tunnel it: ssh -L 8890:127.0.0.1:8890 root@<vps>)
./scripts/whale_os.sh start | status | calibrate | report | logs | stop
```

Pieces, if you want them one at a time:

```bash
python -m consensus.shadow --once     # one cycle, to check wiring
python -m consensus.calibration       # HISTORY table for every series in whale_os.yaml
python -m consensus.shadow            # continuous, every cycle_seconds (default 300)
python -m consensus.hud               # http://127.0.0.1:8890
python -m consensus.report            # plain-text status, go/no-go, per-agent skill
```

The HISTORY agent re-reads `calibration.json` when it changes (checked every
60 s), so rebuilding the table weekly needs no restart.

Config: `config/whale_os.yaml`. Data: `data/whale_os/` (ledger.db,
reliability.db, status.json, calibration.json). All of it is gitignored.

## Go / no-go before any live capital

- At least ~400 resolved fired markets (one per market; repeats don't count)
- ...spread over at least 100 independent events: every strike of one event
  (one city's daily high, one BTC hourly print) settles on the same number, so
  the CI is clustered by event
- Lower bound of that clustered 95% CI on mean P&L per contract > 0 after fees
- All of it priced as a taker (ask + fee, 1 contract). Maker quoting is off in
  shadow because a bid+1 order mostly fills when the price moves against it
- Agents with negative skill against the market get down-weighted
  automatically; drop any that stay negative

## Known assumptions (verify in shadow)

- Weather station coordinates: check each against the market rules text.
- Weather forecast error by lead day (`base_sigma_f`) is a prior, not fitted.
- Crypto uses Coinbase spot as a proxy; Kalshi settles on a CF Benchmarks index.
- Shadow entries are taker-priced (`prefer_maker: false`); if maker quoting is turned
  on, its P&L assumes a fill and is an upper bound.
- Maker fee treatment varies by series; check Kalshi's current fee schedule.
- HISTORY is fitted on past settled markets; its edge is measured only on
  markets settled after the table was built (the shadow ledger), never in-sample.
- With quorum 3 and at most three independent families per market (book, weather
  or crypto, history), a fire needs every family to agree. Expect few fires.
- A family's vote is split across its members that took a side; a member inside
  the deadband (e.g. BOOK at mid) neither helps nor dilutes it.
- Agent skill is scored on each agent's first opinion per market, never on
  vetoed markets, with the market Brier floored at 0.02.
- NWS climate days use local standard time; Open-Meteo days use the local clock.
  During DST the two can disagree by an hour on borderline highs.
