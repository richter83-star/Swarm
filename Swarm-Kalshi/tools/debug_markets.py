# Debug script - inspect real market data from Kalshi
import json
import sys
sys.path.insert(0, 'D:/kalshi-swarm-v4')

from kalshi_agent.kalshi_client import KalshiClient

# Load config
import yaml
with open('D:/kalshi-swarm-v4/config/swarm_config.yaml', 'r') as f:
    config = yaml.safe_load(f)

client = KalshiClient(
    api_key_id=config['api']['key_id'],
    private_key_path=config['api']['private_key_path'],
    base_url=config['api']['base_url'],
    demo_mode=config['api'].get('demo_mode', False)
)

print("Fetching markets with REAL activity filters...")
raw_markets = client.get_markets(status="open")
print(f"Total 'open' markets returned by API: {len(raw_markets)}")

# Find markets with actual money
real_markets = []
for m in raw_markets:
    oi = m.get('open_interest', 0) or 0
    vol = m.get('volume_24h', 0) or 0
    liq = m.get('liquidity', 0) or 0
    yes_bid = m.get('yes_bid', 0) or 0
    yes_ask = m.get('yes_ask', 0) or 0
    
    # Real market criteria
    if oi > 0 and vol > 0 and liq > 0 and yes_bid > 0 and yes_ask > 0:
        real_markets.append({
            'ticker': m.get('ticker'),
            'title': m.get('title', '')[:60],
            'category': m.get('category', 'unknown'),
            'open_interest': oi,
            'volume_24h': vol,
            'liquidity': liq,
            'yes_bid': yes_bid,
            'yes_ask': yes_ask,
            'last_price': m.get('last_price', 0)
        })

print(f"\n=== REAL MARKETS (with OI, volume, liquidity) ===")
print(f"Count: {len(real_markets)}")

if real_markets:
    # Show top 10 by liquidity
    real_markets.sort(key=lambda x: x['liquidity'], reverse=True)
    print("\nTop 10 by liquidity:")
    for m in real_markets[:10]:
        print(f"  ${m['liquidity']/100:.0f} | {m['category'][:12]:12} | {m['ticker'][:40]:40} | {m['title'][:50]}")
else:
    print("\nNo real markets found!")
    print("\nSample of what API returns (first 3):")
    for m in raw_markets[:3]:
        print(f"  Ticker: {m.get('ticker')}")
        print(f"    OI: {m.get('open_interest')}, Vol24h: {m.get('volume_24h')}, Liq: {m.get('liquidity')}")
        print(f"    YES bid/ask: {m.get('yes_bid')}/{m.get('yes_ask')}")
        print(f"    Status: {m.get('status')}, Close time: {m.get('close_time')}")
        print()
