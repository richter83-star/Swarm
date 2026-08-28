# Lessons Learned

## 2026-03-19 — P&L Tracking Bug (Breakeven Bug)

**Pattern:** API fields that are null/missing can silently zero out calculations if the fallback logic treats "found but null" the same as "found with value".

**Root cause:** `_sum_fill_pnl_for_order` in `swarm/bot_runner.py` set `found=True` whenever a fill matched an order_id, regardless of whether `profit_loss` was null. Kalshi fills API omits the `profit_loss` field until market settlement. Result: fill attribution returned `(True, 0)` for every trade, bypassing the correct settlement-level P&L.

**Fix:** Track `has_pnl_data` separately. Only return `found=True` if at least one fill had a non-null `profit_loss`. Otherwise fall through to settlement allocation.

**Diagnostic:** Check `reconciliation_trace` JSON in the learning DB. If `source="fills_by_order"` but `attributed_fill_pnl_cents=0` and `total_ticker_pnl_cents != 0`, fills have null `profit_loss`.

**Rule:** When checking whether an API field was "found", distinguish between "field present and zero" vs "field absent (null)". These are semantically different — null means no data, 0 means explicit zero.
