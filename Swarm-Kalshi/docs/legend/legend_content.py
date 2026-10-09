"""Words for the two legend sheets. Selectors point at the real dashboard elements."""

TUNNEL = ("ssh -L 8890:127.0.0.1:8890 -L 8055:127.0.0.1:8055 "
          "root@vmi3134862.contaboserver.net")

# ─────────────────────────────── GovAward War Room ───────────────────────────────
GOV = {
    "slug": "GOVAWARD",
    "title": "GOVAWARD",
    "subtitle": "WAR ROOM LEGEND",
    "url": "http://127.0.0.1:8055/",
    "outdir": "/root/govaward-alpha/docs/legend",
    "viewport": {"width": 1600, "height": 1000},
    "warm": 14,   # let the first-load replay finish so the radar is calm
    "theme": {
        "paper": "#0b0906", "card": "#120e08", "ink": "#efdcb4", "muted": "#a88d5c", "strong": "#ffd36b",
        "accent": "#ffb000", "line": "rgba(255,176,0,.22)", "frame": "rgba(255,207,90,.55)",
        "pin": "#ffb000", "pinInk": "#140c00",
        "head": "'Chakra Petch', sans-serif", "body": "'JetBrains Mono', monospace", "mono": "'JetBrains Mono', monospace",
        "fonts": "https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@600;700&family=JetBrains+Mono:wght@400;600;700&display=swap",
    },
    "shots": [
        {"lede": "The main screen. Everything here updates every 30 seconds; nothing on it can place a real trade.",
         "items": [
             {"n": 1, "sel": "#mode", "title": "Mode", "text": "engine mode. <b>PAPER</b> = simulated money only."},
             {"n": 2, "sel": "#live", "title": "LIVE lock", "text": "<b>LOCKED</b> unless the server's .env says <code>GOVAWARD_ALLOW_LIVE=yes</code>. Shows red <b>LIVE ARMED</b> if anyone unlocks it."},
             {"n": 3, "sel": "#sniper", "title": "Sniper", "text": "<b>STANDBY</b>, or blinking <b>ARMED</b> 4:45–7:30 PM ET on weekdays while it reads war.gov every 5 min."},
             {"n": 4, "sel": "#cd", "up": ".chip", "title": "DoD drop countdown", "text": "time to the next weekday 5:00 PM ET contract release (clock on its left is ET)."},
             {"n": 5, "sel": "#upd", "title": "Sync", "text": "seconds since the last refresh. <b>SYNC LOST</b> (red) = the dashboard cannot reach the server."},
             {"n": 6, "sel": ".tape", "title": "Tape", "text": "newest contacts scrolling: ticker or awardee · award · buyer · age. Hover to pause."},
             {"n": 7, "sel": "#feeds", "up": ".card", "title": "Feed status", "text": "one verdict per data source, from evidence (see page 3)."},
             {"n": 8, "sel": "#articles", "up": ".card", "title": "DoD drop box", "text": "each war.gov article checked: <b>READ</b> (n awards ≥ $50M), <b>BLOCKED</b>, or <b>PASTED</b>. If blocked: open it, select all, copy, paste, <b>Ingest</b>. Tick <i>dry run</i> to store awards without paper trades."},
             {"n": 9, "sel": ".rbar", "title": "Radar filters", "text": "All · ticker-matched · Red/Orange · ≥ $500M · ≤ 7 days."},
             {"n": 10, "sel": ".corewrap", "title": "Contact radar", "text": "every award of the last ~75 days as a blip. How to read it: page 3.", "dx": 40, "dy": 40},
             {"n": 11, "sel": "#core", "title": "Living core", "text": "the system's pulse. It only reacts to real changes: page 3.", "dx": 60, "dy": 60},
             {"n": 12, "sel": "#contact", "up": ".card", "title": "Contact", "text": "the selected award: amount, award ÷ market cap, age (<b>likely priced in</b> after 7 days), buyer, contract, flags (IDIQ ceiling, modification, option). <b>Stage paper trade</b> needs two clicks."},
             {"n": 13, "sel": "#targets", "up": ".card", "title": "Target board", "text": "ticker-matched awards ranked by award ÷ market cap, fresh ones (≤ 14 days) first. Click a row to select it."},
         ]},
        {"lede": "Scroll down: the event log, your paper account and the edge check.", "scroll": ".bottom",
         "items": [
             {"n": 14, "sel": "#pulselog", "up": ".card", "title": "Pulse log", "text": "every reaction with its ET time. Amber = new contact, orange = warning, red = problem, grey = system. On open it replays what arrived since you last looked."},
             {"n": 15, "sel": "#stats", "up": ".card", "title": "Paper book", "text": "equity, cash, realized P&L, open/pending, win rate. <b>Approve / Reject</b> pending paper orders (two clicks). Simulated money only."},
             {"n": 16, "sel": "#edge", "up": ".card", "title": "Edge check", "text": "does an award move the stock? Average return 1 and 5 trading days after an award, minus the defense ETF (ITA) and SPY. The bar is the 95% range; <b>not distinguishable from 0</b> = no proven edge yet."},
         ]},
    ],
    "reference": f"""
<h2>Contact radar</h2>
<table>
<tr><td>Slice (wedge)</td><td>who bought: Army · Navy/USMC · Air &amp; Space · DLA/Logistics · MDA/DARPA/DoD · Space/Science · Homeland · Civilian</td></tr>
<tr><td>Distance from centre</td><td>age. Rings at 24H, 7D, 30D, 60D. Beyond 30D is shaded: history, almost certainly priced in.</td></tr>
<tr><td>Size</td><td>award dollars (log scale; $50M is the smallest dot).</td></tr>
<tr><td>Shape + colour</td><td><span class="sw" style="background:#ff3b2f;transform:rotate(45deg)"></span>RED diamond · <span class="sw" style="background:#ff7a1a"></span>ORANGE triangle · <span class="sw" style="background:#ffcf5a"></span>YELLOW square · <span class="sw" style="background:#8a6416;border-radius:50%"></span>WATCH circle — materiality, highest first</td></tr>
<tr><td>White outline</td><td>matched to a listed ticker (something you could actually trade).</td></tr>
<tr><td>Expanding bright ring</td><td>posted in the last 48 hours.</td></tr>
<tr><td>Comet from the rim</td><td>a <b>new</b> contact just arrived, flying in on its buyer's bearing; it flares where it lands.</td></tr>
<tr><td>Ring rippling outward</td><td>an event happened (colour = kind, as in the pulse log).</td></tr>
<tr><td>Brackets + dashed line</td><td>the contact you selected. Brightening as the sweep passes is only a highlight.</td></tr>
</table>
<h2>Living core</h2>
<table>
<tr><td>Always breathing</td><td>it is alive and connected; heartbeat about every 2.4 s.</td></tr>
<tr><td>Lobes toward a slice</td><td>award dollars from that buyer in the last 7 days; bigger lobe = more money.</td></tr>
<tr><td>Thin spike toward a slice</td><td>points at the contact you selected.</td></tr>
<tr><td>Surge toward a slice</td><td>a new contact from that buyer.</td></tr>
<tr><td>Surge, no direction</td><td>a system event: order staged, P&amp;L changed, feed changed, sniper armed.</td></tr>
<tr><td>Faster heartbeat, quicker sweep</td><td>sniper armed; energy keeps building over the last 15 minutes before 5 PM ET.</td></tr>
<tr><td>Glitching</td><td>a feed broke, or war.gov blocked today's article.</td></tr>
<tr><td>Colour</td><td>amber = normal · orange = war.gov blocked (paste needed) · red = an active feed is down, or LIVE trading was unlocked</td></tr>
<tr><td>Grey and dim</td><td>the dashboard lost the server (SYNC LOST). It wakes when the server answers.</td></tr>
</table>
<div class="grid2">
<div>
<h2>Feed verdicts</h2>
<table>
<tr><td>LIVE</td><td>fresh data proven by evidence (articles read, awards on file).</td></tr>
<tr><td>STALE</td><td>reachable, but the newest award is old.</td></tr>
<tr><td>DOWN</td><td>the source does not answer from the server.</td></tr>
<tr><td>BLOCKED</td><td>war.gov refused the article: paste it in the drop box.</td></tr>
<tr><td>PARKED</td><td>switched off on purpose (SAM.gov, USAspending); history kept.</td></tr>
</table>
</div>
<div>
<h2>Open it</h2>
<div class="box2">1 · On your PC, keep this window open:
<code class="cmd">{TUNNEL}</code>
2 · Browse to <code>http://localhost:8055</code><br>
3 · Any username; the password is on the VPS:
<code class="cmd">grep GOVAWARD_DASH_PASSWORD /root/govaward-alpha/.env</code>
Old layout: <code>/classic</code>.</div>
<h2>Daily rhythm</h2>
<div class="box2">DoD posts contracts about <b>5:00 PM ET</b>. From 4:45 to 7:30 PM ET the sniper reads the war.gov feed every 5 minutes; awards ≥ $50M fly in as comets. Only newly seen awards can stage a paper trade. LIVE trading stays locked unless you edit the server's .env yourself.</div>
</div>
</div>
""",
}

# ─────────────────────────────────── WHALE-OS ───────────────────────────────────
WHALE = {
    "slug": "WHALE_OS",
    "title": "WHALE-OS",
    "subtitle": "HUD LEGEND",
    "url": "http://127.0.0.1:8890/",
    "outdir": "/root/whale-os/Swarm-Kalshi/docs/legend",
    "viewport": {"width": 1600, "height": 1000},
    "warm": 10,
    "scale": 1.5,
    "motion": "reduce",   # the full 3,400-particle disk is too heavy to screenshot headless
    "theme": {
        "paper": "#000000", "card": "#030806", "ink": "#b8f7cf", "muted": "#5fa77a", "strong": "#efFFf3",
        "accent": "#39ff14", "line": "rgba(57,255,20,.22)", "frame": "rgba(57,255,136,.55)",
        "pin": "#39ff14", "pinInk": "#001a05",
        "head": "'Chakra Petch', sans-serif", "body": "'Share Tech Mono', monospace", "mono": "'Share Tech Mono', monospace",
        "fonts": "https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@600;700&family=Share+Tech+Mono&display=swap",
    },
    "shots": [
        {"lede": "Top of the HUD. Shadow mode: every decision is hypothetical and scored against real settlements; no orders are placed.",
         "items": [
             {"n": 1, "sel": "#crumb", "title": "Top bar", "text": "version, the series and market Jev is on, server load and API latency meters, UTC clock, days running."},
             {"n": 2, "sel": "#rec", "title": "SHADOW", "text": "the mode badge. Shadow = no orders, hypothetical P&L."},
             {"n": 3, "sel": "#tiles", "title": "Agent tiles", "text": "one per agent (BOOK, WHALES, WEATHER, CRYPTO, HISTORY, XVENUE, RESEARCH). Line 1: sources · <b>polling</b> / idle / <b>err %</b> / offline (HISTORY: calibration table size and age). Line 2: signals in 24 h · latency · errors · <b>w</b> = trust weight. Dimmed = switched off in whale_os.yaml."},
             {"n": 4, "sel": "#srcRows", "up": ".p", "title": "Sources", "text": "the public feeds the agents read; bars = recent latency, red bar = an error. A row lights up when it is used."},
             {"n": 5, "sel": "#inspKv", "up": ".p", "title": "Inspector", "text": "the latest single vote: market, agent, p(yes), latency, payload, sources, hash, and the agent's reasoning. <b>Votes · this market</b>: each agent's probability against the market mid (the tick)."},
             {"n": 6, "sel": "#relRows", "up": ".p", "title": "Reliability", "text": "per agent: trust weight, Brier score and <b>skill vs the market</b> on settled markets. Negative skill = worse than the price, so it is down-weighted automatically."},
             {"n": 7, "sel": "#field", "title": "Decision field", "text": "the black hole: agents, data flow and Jev's live call. How to read it: last page.", "dx": 40, "dy": 40},
             {"n": 8, "sel": "#ghost", "title": "Shadow P&L", "text": "total hypothetical P&L in cents per contract, after fees."},
             {"n": 9, "sel": "#mission", "title": "Rule strip", "text": "Jev fires only when 3 independent families agree, shadow mode, no orders."},
             {"n": 10, "sel": "#jevP", "title": "JEV", "text": "the decider. Big number = probability for the side it likes. Badge: <b>IDLE / HOLD / VETO / FIRE · BUY YES|NO</b>. <i>agree</i> = families agreeing / quorum. Segments: one per agent (green = YES vs market, amber = NO, dim = flat, dark = abstain). Prior → posterior, entry price, net edge, and the gates (✓/✗)."},
             {"n": 11, "sel": "#book", "title": "Book", "text": "live order-book ladder for the market Jev holds."},
             {"n": 12, "sel": "#pnlNum", "up": ".p", "title": "PNL", "text": "shadow P&L per contract with its running curve."},
         ]},
        {"lede": "Scroll down: the four live charts.", "scroll": "#candCv",
         "items": [
             {"n": 13, "sel": "#candCv", "up": ".p", "title": "Candles", "text": "hourly candles of the market Jev is looking at (last, mid, volume underneath)."},
             {"n": 14, "sel": "#fanCv", "up": ".p", "title": "Drift vs noise", "text": "120 resampled paths of the shadow P&L: is it real drift or luck? <b>P(PROFIT)</b>, median and 5–95% range. Until enough fires settle it says how many are needed."},
             {"n": 15, "sel": "#bayesCv", "up": ".p", "title": "Bayes", "text": "the market's price (prior) next to the pooled agents' probability (posterior). The gap is the edge; GATE says whether it clears fees."},
             {"n": 16, "sel": "#mxCv", "up": ".p", "title": "Consensus matrix", "text": "rows = agents, columns = recent evaluations: green YES, amber NO, dim flat, dark abstain. Bottom strip = Jev's action (green fire, red veto, dark hold)."},
         ]},
        {"lede": "Bottom of the HUD: the shadow trade log, the agent log and the totals.", "scroll": "#tape",
         "items": [
             {"n": 17, "sel": "#tape", "title": "Trades_out.csv", "text": "every shadow fire, newest first: families agreeing, which agents, side and price, probability, P&L, open / settled / void."},
             {"n": 18, "sel": "#log", "up": ".p", "title": "Tail -f agents.log", "text": "the agent stream replayed: READ (data pulled), VOTE, FLAG (veto), FIRE."},
             {"n": 19, "sel": "#stats", "title": "Totals", "text": "signals read, fired, held, vetoes, shadow P&L, win rate, markets, cycle time; each with its trend line."},
         ]},
    ],
    "reference": f"""
<h2>Reading the black hole</h2>
<table>
<tr><td>Particles flowing in</td><td>an agent pulled data and voted: they travel source box → agent node → core. Colour = the agent's family.</td></tr>
<tr><td>Agent nodes</td><td>the glowing dots around the hole, one per enabled agent; they flare when they vote.</td></tr>
<tr><td>Consensus ring</td><td>the segmented ring hugging the horizon: one segment per agent (two-letter label). <span class="sw" style="background:#39ff14"></span>green = leans YES vs the market · <span class="sw" style="background:#ffb020"></span>amber = leans NO · <span class="sw" style="background:#2e6b45"></span>dim = flat (within 2c) · dark = abstained</td></tr>
<tr><td>Inside the horizon</td><td>Jev's live call: <b>FIRE</b> (green) / <b>VETO</b> (red) / HOLD / IDLE, with <i>agreeing/quorum · edge in cents</i>.</td></tr>
<tr><td>Jets from the poles</td><td>faint and breathing normally; they blaze when Jev fires.</td></tr>
<tr><td>Rings bursting from the core</td><td>a decision: green = fire, red = veto, grey-green = hold.</td></tr>
<tr><td>Disk, stars, lensing, photon ring</td><td>atmosphere only: they carry no data.</td></tr>
</table>
<div class="grid2">
<div>
<h2>How Jev decides</h2>
<table>
<tr><td>Families</td><td>agents reading the same data count once: BOOK + WHALES = one microstructure vote; WEATHER or CRYPTO = model; HISTORY = calibration.</td></tr>
<tr><td>Fire</td><td>3 families agree, at least 3 voters, and the edge is ≥ 3c per contract after Kalshi fees, priced as a taker.</td></tr>
<tr><td>Veto</td><td>a hard rule blocked it whatever the votes: spread too wide, one-sided book, price outside the band, too soon or too far from close, ambiguous rules text, or a blocked series.</td></tr>
<tr><td>Hold</td><td>not enough agreement or edge.</td></tr>
</table>
<h2>Before any real money</h2>
<div class="box2">Go / no-go needs <b>≥ 400 settled fired markets</b> over <b>≥ 100 independent events</b>, and the lower end of the 95% range of P&amp;L per contract <b>above 0 after fees</b>. Until then this is evidence-gathering. <code>./scripts/whale_os.sh report</code> prints where it stands.</div>
</div>
<div>
<h2>Open it</h2>
<div class="box2">1 · On your PC, keep this window open (it opens GovAward too):
<code class="cmd">{TUNNEL}</code>
2 · Browse to <code>http://localhost:8890</code><br>
If the page stops updating, the tunnel closed (PC slept, window closed): run step 1 again. WHALE-OS itself keeps running on the VPS.</div>
<h2>On the VPS</h2>
<div class="box2"><code>cd /root/whale-os/Swarm-Kalshi</code><br>
<code>./scripts/whale_os.sh status</code> · <code>report</code> · <code>logs</code><br>
Auto-restart check every 10 minutes; HISTORY table rebuilt every Sunday 04:17 server time.</div>
</div>
</div>
""",
}
