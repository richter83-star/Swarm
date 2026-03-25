# Kalshi Swarm — Roadmap, Milestones & Deposit Schedule

## The Goal
- **Realistic long-term:** $10,000/month passive income
- **Dream goal:** $30,000/month
- **Exit strategy:** Sell the program once proven at scale

---

## Account Size → Monthly Income Map

| Account Value | @ 10%/mo | @ 15%/mo | @ 20%/mo |
|---------------|----------|----------|----------|
| $1,500        | $150     | $225     | $300     |
| $5,000        | $500     | $750     | $1,000   |
| $10,000       | $1,000   | $1,500   | $2,000   |
| $25,000       | $2,500   | $3,750   | $5,000   |
| $50,000       | $5,000   | $7,500   | $10,000  |
| $75,000       | $7,500   | $11,250  | $15,000  |
| $100,000      | $10,000  | $15,000  | $20,000  |
| $150,000      | $15,000  | $22,500  | $30,000  |
| $200,000      | $20,000  | $30,000  | $40,000  |

**Realistic $10K/month target account: $65,000–$100,000**
**Dream $30K/month target account: $150,000–$300,000**

---

## Milestones (Account Value Based)

### 🟡 MILESTONE 1 — $1,500 (Proof of Concept)
- **Monthly income:** ~$150–300
- **Action:** Confirm strategy is working. Review win rate.
- **System:** Current setup, no changes needed.
- **Guardrail change:** None yet.

### 🟢 MILESTONE 2 — $5,000 (First Real Money)
- **Monthly income:** ~$500–1,000
- **Action:** Reinvest all profits. Pause deposits if compounding covers it.
- **System:** Consider raising max_open_positions 6→8 per bot.
- **Guardrail change:** Loosen if 50+ clean trades, 55%+ win rate, 14 days positive.

### 🟢 MILESTONE 3 — $10,000 (Meaningful Passive Income)
- **Monthly income:** ~$1,000–2,000
- **Action:** Can start withdrawing ~20% monthly. Reinvest 80%.
- **System:** Raise max_position_pct 7%→8% (larger positions = faster growth).
- **Note:** Start documenting system for eventual sale.

### 🔵 MILESTONE 4 — $25,000
- **Monthly income:** ~$2,500–5,000
- **Action:** Evaluate adding a second Kalshi account or porting to Polymarket.
- **System:** Watch for liquidity walls on smaller markets. May need to diversify market selection.
- **Guardrail change:** Raise max_open_positions to 10 per bot.

### 🔵 MILESTONE 5 — $50,000
- **Monthly income:** ~$5,000–10,000
- **Action:** Begin preparing program for sale. Build documentation.
- **System:** At 7% per trade = $3,500/trade. Liquidity on some Kalshi markets may limit. Start routing larger trades to higher-liquidity markets only.
- **Note:** At this scale, Kalshi alone may not absorb all capital. Multi-platform strategy needed.

### 🚀 MILESTONE 6 — $75,000
- **Monthly income:** ~$7,500–15,000
- **Action:** $10K/month realistic goal is in range. Consider selling access to the system.
- **System:** May need to cap per-trade size at $1,000 to avoid moving the market on smaller Kalshi contracts.

### 🚀 MILESTONE 7 — $100,000 (Realistic Long-Term Goal Achieved)
- **Monthly income:** ~$10,000–20,000 ✅
- **Action:** Program is proven at scale. Sell access or license it.
- **Valuation:** A system generating $10K+/month has sale value of $120,000–$500,000+ (12–50× monthly).

### 💎 MILESTONE 8 — $200,000+ (Dream Goal Range)
- **Monthly income:** ~$20,000–40,000
- **Action:** $30K/month achieved. Full financial freedom.
- **Note:** At this scale, running multiple accounts across Kalshi, Polymarket, and other prediction markets simultaneously.

---

## Deposit Schedule (Phase 1 — Proving the System)

**Clean data window starts:** March 25, 2026

| Date | Action | Condition |
|------|--------|-----------|
| **Apr 8, 2026** | +$500 | Win rate ≥55% over 50+ resolved trades in 20-65¢ band |
| **Apr 22** | +$300 | System running clean (net positive 2-week window) |
| **May 6** | +$300 | Same |
| **May 20** | +$300 | Same |
| **Jun 3** | +$300 | Same |
| **Jun 17** | +$300 | Same |
| **Jul 1** | +$300 | Same |
| **Jul 15** | +$300 | Same |
| **Jul 29** | +$300 | Same |
| **Aug 12** | +$300 | Same |
| **Aug 26** | +$300 | Same |
| **Sep 9** | +$300 | Same |
| **Sep 23** | +$300 | Same |
| **Oct 7** | +$300 | Same |
| **Oct 21** | +$300 | Same |

**Total Phase 1 deposits: $4,750** (over 7 months)
**Projected account Nov 1:** $7,200–$11,200 depending on return rate

---

## Phase 2 — Scaling (Nov 2026 onward)
Once Phase 1 is complete and account is $7K–$11K:
- Reinvest 100% of profits
- Increase deposits to $500/bi-weekly if return rate is holding
- Target Milestone 3 ($10K account) by Q1 2027
- Target Milestone 4 ($25K account) by Q3 2027
- Target Milestone 5 ($50K account) by Q1 2028

---

## Compounding Projection to Dream Goal

| Return Rate | Time to $100K | Time to $200K |
|-------------|---------------|---------------|
| 10%/month   | ~3.5 years    | ~4.5 years    |
| 15%/month   | ~2.5 years    | ~3 years      |
| 20%/month   | ~2 years      | ~2.5 years    |

*Starting from Phase 1 completion (~$9K in Nov 2026), reinvesting all profits.*

---

## Sale Valuation Benchmarks

When the system is proven and generating consistent returns, sale value:
- At $5K/month proven: **$60K–$250K** (12–50× monthly revenue)
- At $10K/month proven: **$120K–$500K**
- At $30K/month proven: **$360K–$1.5M+**

The longer the track record, the higher the multiple.

---

## Key Risk Controls (Never Change Without Review)
- `max_drawdown_pct: 10%` — never raise above 15%
- `min_balance_cents: 500` — hard floor, never remove
- `max_entry_price_cents: 65` — proven profitable ceiling
- `min_entry_price_cents: 20` — proven profitable floor
- `max_position_pct: 7%` — raise only at Milestone 3+

---

## Notes
- Session crons expire every 3 days — recreate keep-alive on each session start
- All data before March 25, 2026 is suspect (broken period)
- Reminder cron set for March 28 to recreate April 8 deposit trigger
