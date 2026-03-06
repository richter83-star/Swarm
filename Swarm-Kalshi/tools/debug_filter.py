import yaml
import sys
sys.path.insert(0, '.')
from kalshi_agent.kalshi_client import KalshiClient
from kalshi_agent.market_scanner import MarketScanner

# Load config
with open('config/swarm_config.yaml') as f:
    cfg = yaml.safe_load(f)

trading_cfg = cfg['trading']
print('Trading config:')
print(f"  min_liquidity_cents: {trading_cfg.get('min_liquidity_cents', 0)}")
print(f"  min_volume_24h: {trading_cfg.get('min_volume_24h', 0)}")
print(f"  min_hours_to_expiry: {trading_cfg.get('min_hours_to_expiry', 0)}")
print(f"  max_hours_to_expiry: {trading_cfg.get('max_hours_to_expiry', float('inf'))}")

# Create client
client = KalshiClient(
    api_key_id=cfg['api']['key_id'],
    private_key_path=cfg['api']['private_key_path'],
    base_url=cfg['api']['base_url']
)

# Create scanner
scanner = MarketScanner(client, trading_cfg)

# Fetch markets
markets = client.get_markets(status='open', limit=100)
print(f'\nFetched {len(markets)} markets')

# Check what filters are failing
passed_count = 0
activity_count = 0
for m in markets[:100]:
    ticker = m.get('ticker', 'N/A')
    vol = m.get('volume_24h', 0) or 0
    liq = m.get('liquidity', 0) or 0
    oi = m.get('open_interest', 0) or 0
    yes_bid = m.get('yes_bid') or 0
    yes_ask = m.get('yes_ask') or 0
    last_price = m.get('last_price') or 0
    
    # Calculate mid price like the scanner does
    if yes_bid and yes_ask:
        mid = (yes_bid + yes_ask) / 2.0
    else:
        mid = float(last_price)
    
    has_activity = (oi > 0 or vol > 0 or liq > 0)
    
    # Check filter conditions
    checks = {
        'has_activity': has_activity,
        'liq >= min': liq >= trading_cfg.get('min_liquidity_cents', 0),
        'vol >= min': vol >= trading_cfg.get('min_volume_24h', 0),
        'mid > 0': mid > 0,
        'mid < 100': mid < 100,
    }
    
    passed = all(checks.values())
    
    # Count passing/failing markets
    if passed:
        passed_count += 1
        print(f'PASS: {ticker}: vol={vol}, liq={liq}, oi={oi}, mid={mid:.1f}')
    elif has_activity:
        activity_count += 1
        if activity_count <= 10:  # Show first 10 with activity that failed
            print(f'FAIL (has activity): {ticker}: vol={vol}, liq={liq}, oi={oi}, mid={mid:.1f}')
            print(f'  Checks: {checks}')

print(f'\nSummary: {passed_count} passed, {activity_count} have activity but failed')
