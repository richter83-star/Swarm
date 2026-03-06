import sys
sys.path.insert(0, 'D:/kalshi-swarm-v4')

import yaml
from kalshi_agent.market_scanner import MarketScanner, MarketOpportunity
from kalshi_agent.kalshi_client import KalshiClient

# Load actual config
with open('D:/kalshi-swarm-v4/config/swarm_config.yaml', 'r') as f:
    config = yaml.safe_load(f)

trading_cfg = config.get('trading', {})
print('Config thresholds:')
print(f"  min_liquidity_cents: {trading_cfg.get('min_liquidity_cents', 'NOT SET')}")
print(f"  min_volume_24h: {trading_cfg.get('min_volume_24h', 'NOT SET')}")

# Create mock client
client = KalshiClient(
    api_key_id=config['api']['key_id'],
    private_key_path=config['api']['private_key_path'],
    base_url=config['api']['base_url'],
    demo_mode=config['api'].get('demo_mode', False)
)

scanner = MarketScanner(client, trading_cfg)

# Fetch and test filter
raw_markets = client.get_markets(status="open")
print(f"\nFetched {len(raw_markets)} markets")

# Check first market with activity
for m in raw_markets:
    oi = m.get('open_interest') or 0
    vol = m.get('volume_24h') or 0
    liq = m.get('liquidity') or 0
    
    if oi > 0 or vol > 0 or liq > 0:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        opp = scanner._parse_market(m, now)
        
        print(f"\n=== First market with activity ===")
        print(f"Ticker: {opp.ticker}")
        print(f"OI: {opp.open_interest}, Vol24h: {opp.volume_24h}, Liq: {opp.liquidity}")
        print(f"Hours to expiry: {opp.hours_to_expiry:.1f}")
        print(f"Mid price: {opp.mid_price}")
        
        passes = scanner._passes_filters(opp)
        print(f"Passes filters: {passes}")
        
        # Show why
        min_liq = trading_cfg.get("min_liquidity_cents", 0)
        min_vol = trading_cfg.get("min_volume_24h", 0)
        min_hrs = trading_cfg.get("min_hours_to_expiry", 0)
        max_hrs = trading_cfg.get("max_hours_to_expiry", float("inf"))
        
        has_activity = (opp.open_interest > 0 or opp.volume_24h > 0 or opp.liquidity > 0)
        print(f"\nFilter breakdown:")
        print(f"  has_activity (OI|vol|liq>0): {has_activity}")
        print(f"  liq >= min_liq ({opp.liquidity} >= {min_liq}): {opp.liquidity >= min_liq}")
        print(f"  vol >= min_vol ({opp.volume_24h} >= {min_vol}): {opp.volume_24h >= min_vol}")
        print(f"  hrs in range ({min_hrs} <= {opp.hours_to_expiry:.1f} <= {max_hrs}): {min_hrs <= opp.hours_to_expiry <= max_hrs}")
        print(f"  mid price valid (0 < {opp.mid_price} < 100): {0 < opp.mid_price < 100}")
        break
else:
    print("\nNo markets with any activity found!")
