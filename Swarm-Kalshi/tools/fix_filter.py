import re

with open('D:/kalshi-swarm-v4/kalshi_agent/market_scanner.py', 'r') as f:
    content = f.read()

# Replace the strict filter with relaxed one
old_filter = '''    def _passes_filters(self, opp: MarketOpportunity) -> bool:
        min_liq = self.cfg.get("min_liquidity_cents", 0)
        min_vol = self.cfg.get("min_volume_24h", 0)
        min_hrs = self.cfg.get("min_hours_to_expiry", 0)
        max_hrs = self.cfg.get("max_hours_to_expiry", float("inf"))

        # === REAL MARKET FILTERS ===
        # Must have actual money in the market
        if opp.open_interest <= 0:
            logger.debug("Skipping %s: no open interest (dead market)", opp.ticker)
            return False
        if opp.liquidity <= 0:
            logger.debug("Skipping %s: zero liquidity", opp.ticker)
            return False
        if opp.volume_24h <= 0:
            logger.debug("Skipping %s: no volume (no trades today)", opp.ticker)
            return False
        # Must have actual bid/ask spread (active orderbook)
        if opp.yes_bid <= 0 or opp.yes_ask <= 0:
            logger.debug("Skipping %s: inactive YES side", opp.ticker)
            return False
        # ============================

        if opp.liquidity < min_liq:
            return False
        if opp.volume_24h < min_vol:
            return False
        if opp.hours_to_expiry < min_hrs:
            return False
        if opp.hours_to_expiry > max_hrs:
            return False
        if opp.mid_price <= 0 or opp.mid_price >= 100:
            return False
        return True'''

new_filter = '''    def _passes_filters(self, opp: MarketOpportunity) -> bool:
        min_liq = self.cfg.get("min_liquidity_cents", 0)
        min_vol = self.cfg.get("min_volume_24h", 0)
        min_hrs = self.cfg.get("min_hours_to_expiry", 0)
        max_hrs = self.cfg.get("max_hours_to_expiry", float("inf"))

        # === REAL MARKET FILTERS ===
        # Market must have SOME real activity (OI OR volume OR liquidity)
        has_activity = (opp.open_interest > 0 or opp.volume_24h > 0 or opp.liquidity > 0)
        if not has_activity:
            return False
        # ============================

        if opp.liquidity < min_liq:
            return False
        if opp.volume_24h < min_vol:
            return False
        if opp.hours_to_expiry < min_hrs:
            return False
        if opp.hours_to_expiry > max_hrs:
            return False
        if opp.mid_price <= 0 or opp.mid_price >= 100:
            return False
        return True'''

if old_filter in content:
    content = content.replace(old_filter, new_filter)
    with open('D:/kalshi-swarm-v4/kalshi_agent/market_scanner.py', 'w') as f:
        f.write(content)
    print('Fixed! Changed to OR logic (any activity)')
else:
    print('Could not find exact filter to replace')
    # Let's just overwrite the whole file section
    # Find the _passes_filters method
    start = content.find('    def _passes_filters(self, opp: MarketOpportunity) -> bool:')
    if start == -1:
        print('ERROR: Could not find _passes_filters method')
        exit(1)
    
    # Find the next method or end of class
    next_def = content.find('\n    def ', start + 1)
    if next_def == -1:
        next_def = len(content)
    
    # Replace the method
    new_content = content[:start] + new_filter + content[next_def:]
    with open('D:/kalshi-swarm-v4/kalshi_agent/market_scanner.py', 'w') as f:
        f.write(new_content)
    print('Fixed by method replacement')
