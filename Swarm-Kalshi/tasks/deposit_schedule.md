# Kalshi Swarm — Deposit Schedule & Trigger Events

## Goal
$1,000/month passive income → scale to sell the program.
Target account size to hit $1,000/month: ~$1,200–1,500.

---

## Deposit Trigger Schedule

### Trigger 1 — PROOF MILESTONE (April 8, 2026)
**Condition:** 2 clean weeks of data from March 25, 2026 onward
**Action:** Review win rate + PnL. If win rate ≥ 55% over 50+ resolved trades → **ADD $500**
**What to check:**
- Win rate across all 4 bots in 20-65¢ price band
- Net PnL positive over the 2-week window
- No sustained crash loops or silent failures

### Trigger 2 — RECURRING DEPOSITS (Every 2 weeks after Trigger 1)
**Schedule:** April 22, May 6, May 20, June 3, June 17...
**Amount:** $300 each deposit
**Condition:** System still running clean (no sustained losses, bots active)

---

## Compound Projection (if strategy holds)

| Date | Account (organic) | Account (with deposits) | Est. Monthly Income |
|------|-------------------|------------------------|---------------------|
| Mar 25 | $50 | $50 | ~$15 |
| Apr 8  | ~$65 | $565 (+ $500) | ~$170 |
| Apr 22 | ~$85 | $900 (+ $300) | ~$270 |
| May 6  | ~$110 | $1,240 (+ $300) | ~$370 |
| May 20 | ~$145 | $1,620 (+ $300) | ~$490 |
| Jun 3  | ~$190 | $2,010 (+ $300) | ~$600 |
| Jun 17 | ~$250 | $2,420 (+ $300) | ~$730 |
| Jul 1  | ~$325 | $2,870 (+ $300) | ~$860 |
| Jul 15 | ~$425 | $3,370 (+ $300) | **~$1,000+** |

*Organic compounding assumes ~1%/day net. Actual rate TBD from clean data.*

---

## Guardrail Loosening Criteria (when to remove remaining limits)
- Win rate ≥ 55% over 50+ resolved trades
- 14+ consecutive days net positive PnL
- Max drawdown in any single day < 10%
- No crash loops in prior 7 days

---

## Notes
- Clean data period starts: March 25, 2026 (after price band fix 20-65¢)
- All data before March 25 is suspect (Anthropic key broken, crash loops, wrong price zones)
- Session crons expire every 3 days — recreate keep-alive and reminder crons on each session start
