"""
verify_api_connection.py
========================

Test script to verify Kalshi API is working and check actual balance.
"""

import yaml
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, "D:\\kalshi-swarm-v4")

from kalshi_agent.kalshi_client import KalshiClient

def test_api_connection():
    """Test the API connection and report balance"""
    
    print("="*60)
    print("KALSHI API VERIFICATION")
    print("="*60)
    
    # Load config
    config_path = Path("D:\\kalshi-swarm-v4\\config\\swarm_config.yaml")
    
    if not config_path.exists():
        print(f"❌ Config file not found: {config_path}")
        return False
    
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    api_config = config.get('api', {})
    
    key_id = api_config.get('key_id')
    private_key_path = api_config.get('private_key_path', 'keys/kalshi-private.key')
    base_url = api_config.get('base_url', 'https://api.elections.kalshi.com/trade-api/v2')
    demo_mode = api_config.get('demo_mode', False)
    
    print(f"\n📋 Configuration:")
    print(f"   Key ID: {key_id[:20]}..." if key_id else "   Key ID: NOT SET")
    print(f"   Base URL: {base_url}")
    print(f"   Demo Mode: {demo_mode}")
    print(f"   Private Key: {private_key_path}")
    
    # Check if key file exists
    key_file = Path("D:\\kalshi-swarm-v4") / private_key_path
    if not key_file.exists():
        print(f"\n❌ Private key file NOT FOUND: {key_file}")
        print("   Bots cannot authenticate without this key!")
        return False
    else:
        print(f"\n✅ Private key file found: {key_file}")
    
    # Create client
    print(f"\n🔌 Connecting to Kalshi API...")
    
    try:
        client = KalshiClient(
            api_key_id=key_id,
            private_key_path=str(key_file),
            base_url=base_url,
            demo_mode=demo_mode
        )
        
        # Test 1: Get exchange status
        print("\n📊 Test 1: Exchange Status")
        try:
            status = client.get_exchange_status()
            print(f"   ✅ Exchange is OPEN")
            print(f"   Trading active: {status.get('trading_active', 'unknown')}")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
        
        # Test 2: Get balance
        print("\n💰 Test 2: Account Balance")
        try:
            balance_data = client.get_balance()
            balance_cents = balance_data.get('balance', 0)
            balance_dollars = balance_cents / 100
            
            print(f"   ✅ Balance retrieved successfully")
            print(f"   Balance: {balance_cents} cents = ${balance_dollars:.2f}")
            
            if balance_cents == 0:
                print(f"\n   ⚠️  WARNING: Balance is $0.00")
                print(f"      This means either:")
                print(f"      1. You're in DEMO mode and demo account has no funds")
                print(f"      2. Your real account has no funds")
                print(f"      3. The API key is for a different account than you checked")
            else:
                print(f"\n   🎉 SUCCESS: Account has ${balance_dollars:.2f}")
                
        except Exception as e:
            print(f"   ❌ Failed to get balance: {e}")
            return False
        
        # Test 3: Get positions
        print("\n📈 Test 3: Open Positions")
        try:
            positions = client.get_positions()
            print(f"   ✅ Found {len(positions)} open positions")
            if positions:
                for pos in positions[:3]:  # Show first 3
                    ticker = pos.get('market_ticker', 'unknown')
                    side = pos.get('side', 'unknown')
                    size = pos.get('position', 0)
                    print(f"      - {ticker}: {side} x{size}")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
        
        # Test 4: Get markets
        print("\n🏪 Test 4: Market Access")
        try:
            markets = client.get_markets(limit=5)
            print(f"   ✅ Successfully fetched {len(markets)} markets")
            if markets:
                for m in markets[:3]:
                    print(f"      - {m.get('ticker', 'unknown')}: {m.get('title', 'unknown')[:50]}...")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
        
        print("\n" + "="*60)
        print("VERIFICATION COMPLETE")
        print("="*60)
        
        return True
        
    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {e}")
        print("   The API connection failed entirely.")
        print("   Check:")
        print("   - API key is correct")
        print("   - Private key file is valid")
        print("   - Base URL is correct")
        print("   - Internet connection")
        return False

if __name__ == "__main__":
    success = test_api_connection()
    sys.exit(0 if success else 1)
