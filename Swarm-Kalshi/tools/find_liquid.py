import sys
sys.path.insert(0, 'D:/kalshi-swarm-v4')

from kalshi_agent.kalshi_client import KalshiClient
import yaml

with open('config/swarm_config.yaml') as f:
    cfg = yaml.safe_load(f)

client = KalshiClient(
    api_key_id=cfg['api']['key_id'],
    private_key_path=cfg['api']['private_key_path'],
    base_url=cfg['api']['base_url'],
    demo_mode=cfg['api'].get('demo_mode', False)
)

print("Scanning for markets with LIQUIDITY > 0...")
markets = client.get_markets(status="open")

with_liq = []
for m in markets[:1000]:  # Check first 1000
    liq = m.get('liquidity') or 0
    if liq > 0:
        with_liq.append({
            'ticker': m.get('ticker'),
            'liq': liq,
            'oi': m.get('open_interest') or 0,
            'vol': m.get('volume_24h') or 0
        })

print(f"\nFound {len(with_liq)} markets with liquidity > 0 in first 1000")
if with_liq:
    print("\nTop 10 by liquidity:")
    with_liq.sort(key=lambda x: x['liq'], reverse=True)
    for m in with_liq[:10]:
        print(f"  ${m['liq']/100:.2f} | OI={m['oi']} | {m['ticker'][:50]}")
else:
    print("\nNo markets with liquidity found in first 1000!")
    print("This suggests either:")
    print("  1. Wrong API endpoint (demo vs live)")
    print("  2. All markets are dormant")
    print("  3. Need different time of day for active trading")
