"""
debug_bot_balance.py
====================

Debug script to trace why bots are seeing $0 balance.
"""

import sys
import yaml
from pathlib import Path

sys.path.insert(0, "D:\\kalshi-swarm-v4")

from kalshi_agent.kalshi_client import KalshiClient, KalshiAPIError

def debug_bot_balance(bot_name):
    """Debug a specific bot's balance fetch"""
    
    print(f"\n{'='*60}")
    print(f"DEBUGGING {bot_name.upper()} BALANCE")
    print(f"{'='*60}\n")
    
    project_root = Path("D:\\kalshi-swarm-v4")
    
    # Load configs
    swarm_config_path = project_root / "config" / "swarm_config.yaml"
    bot_config_path = project_root / "config" / f"{bot_name}_config.yaml"
    
    print(f"1. Loading configs:")
    print(f"   Swarm config: {swarm_config_path}")
    print(f"   Bot config: {bot_config_path}")
    
    with open(swarm_config_path) as f:
        swarm_cfg = yaml.safe_load(f)
    
    with open(bot_config_path) as f:
        bot_cfg = yaml.safe_load(f)
    
    # Merge configs (as BotRunner does)
    cfg = swarm_cfg.copy()
    cfg.update(bot_cfg)
    
    api_cfg = cfg.get('api', {})
    
    print(f"\n2. API Configuration:")
    print(f"   Key ID: {api_cfg.get('key_id', 'NOT SET')[:30]}...")
    print(f"   Base URL: {api_cfg.get('base_url', 'NOT SET')}")
    print(f"   Demo Mode: {api_cfg.get('demo_mode', 'NOT SET')}")
    print(f"   Private Key Path: {api_cfg.get('private_key_path', 'NOT SET')}")
    
    key_path = project_root / api_cfg.get('private_key_path', 'keys/kalshi-private.key')
    print(f"   Full Key Path: {key_path}")
    print(f"   Key Exists: {key_path.exists()}")
    
    # Create client
    print(f"\n3. Creating KalshiClient...")
    try:
        client = KalshiClient(
            api_key_id=api_cfg['key_id'],
            private_key_path=str(key_path),
            base_url=api_cfg['base_url'],
            demo_mode=api_cfg.get('demo_mode', True),
        )
        print(f"   ✅ Client created successfully")
    except Exception as e:
        print(f"   ❌ Failed to create client: {e}")
        return
    
    # Fetch balance
    print(f"\n4. Fetching balance...")
    try:
        balance_data = client.get_balance()
        print(f"   ✅ Balance data received")
        print(f"   Raw response: {balance_data}")
        
        balance = balance_data.get('balance', 0)
        print(f"\n5. Balance Result:")
        print(f"   Balance (cents): {balance}")
        print(f"   Balance (dollars): ${balance/100:.2f}")
        
        if balance == 0:
            print(f"\n   ⚠️  WARNING: Balance is 0!")
            print(f"      This means the API returned 0, not that the fetch failed.")
            
    except KalshiAPIError as e:
        print(f"   ❌ KalshiAPIError: {e}")
        print(f"      Status code: {e.status_code}")
        print(f"      Message: {e.message}")
    except Exception as e:
        print(f"   ❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Test all 4 bots
    for bot in ["sentinel", "oracle", "pulse", "vanguard"]:
        debug_bot_balance(bot)
        print("\n" + "="*60)
        print("Press Enter to continue to next bot...")
        print("="*60)
        input()
