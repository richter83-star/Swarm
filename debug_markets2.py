# Debug script - inspect real market data from Kalshi
import json
import sys
sys.path.insert(0, 'D:/kalshi-swarm-v4')

from kalshi_agent.kalshi_client import KalshiClient

# Load config - simplified
api_key_id = '7e122750-a5cd-4474-8bb3-e701d55bc440'
private_key_path = 'keys/kalshi-private.key'
base_url = 'https://api.elections.kalshi.com/trade-api/v2'

client = KalshiClient(
    api_key_id=api_key_id,
    private_key_path=private_key_path,
    base_url=base_url,
    demo_mode=False
)

print("Looking for ANY markets with non-zero values...")
raw_markets = client.get_markets(status="open")
print(f"Total markets: {len(raw_markets)}")

# Find ANY market with activity
active_markets = []
for m in raw_markets[:1000]:  # Check first 1000
    oi = m.get('open_interest') or 0
    vol = m.get('volume_24h') or 0
    liq = m.get('liquidity') or 0
    
    if oi > 0 or vol > 0 or liq > 0:
        active_markets.append({
            'ticker': m.get('ticker'),
            'oi': oi,
            'vol': vol,
            'liq': liq,
            'yes_bid': m.get('yes_bid'),
            'yes_ask': m.get('yes_ask')
        })

if active_markets:
    print(f"\nFound {len(active_markets)} markets with activity!")
    for m in active_markets[:5]:
        print(f"  {m['ticker']}: OI={m['oi']}, Vol={m['vol']}, Liq={m['liq']}")
else:
    print("\nNo active markets in first 1000 checked.")
    print("\nAll markets appear inactive. This is either:")
    print("  1. Sandbox/demo environment returning dummy data")
    print("  2. Wrong API endpoint")
    print("  3. Markets are expired/unlisted but still 'open'")
    
    # Check a sample of field values
    print("\nSample market fields:")
    m = raw_markets[0]
    for k, v in m.items():
        print(f"  {k}: {v}")

# Try fetching specific series
print("\n\nTrying series lookup...")
try:
    series_list = client.get_series_list()
    print(f"Available series: {len(series_list)}")
    if series_list:
        print(f"First few: {[s.get('ticker') for s in series_list[:5]]}")
except Exception as e:
    print(f"Series fetch failed: {e}")
