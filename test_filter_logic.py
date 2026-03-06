import sys
sys.path.insert(0, 'D:/kalshi-swarm-v4')

# Simulate what the scanner does
from datetime import datetime, timezone

class MockOpp:
    def __init__(self, oi, vol, liq, hrs, mid):
        self.open_interest = oi
        self.volume_24h = vol
        self.liquidity = liq
        self.hours_to_expiry = hrs
        self.mid_price = mid
        self.ticker = "TEST"

# Config values from swarm_config.yaml
trading_cfg = {
    'min_liquidity_cents': 100,
    'min_volume_24h': 0,
    'min_hours_to_expiry': 0.5,
    'max_hours_to_expiry': 720,
}

def passes_filters(opp):
    min_liq = trading_cfg.get("min_liquidity_cents", 0)
    min_vol = trading_cfg.get("min_volume_24h", 0)
    min_hrs = trading_cfg.get("min_hours_to_expiry", 0)
    max_hrs = trading_cfg.get("max_hours_to_expiry", float("inf"))

    # === REAL MARKET FILTERS ===
    has_activity = (opp.open_interest > 0 or opp.volume_24h > 0 or opp.liquidity > 0)
    if not has_activity:
        return False, "no_activity"

    if opp.liquidity < min_liq:
        return False, f"liq_too_low ({opp.liquidity} < {min_liq})"
    if opp.volume_24h < min_vol:
        return False, f"vol_too_low ({opp.volume_24h} < {min_vol})"
    if opp.hours_to_expiry < min_hrs:
        return False, f"expires_too_soon ({opp.hours_to_expiry:.1f}h < {min_hrs}h)"
    if opp.hours_to_expiry > max_hrs:
        return False, f"expires_too_late"
    if opp.mid_price <= 0 or opp.mid_price >= 100:
        return False, f"invalid_price ({opp.mid_price})"
    return True, "pass"

# Test the 58 markets we found earlier (OI only, no vol/liq)
test_cases = [
    MockOpp(533, 0, 0, 24, 50),  # OI=533, 24h to expiry, mid=50
    MockOpp(101, 0, 0, 24, 50),  # OI=101
    MockOpp(74, 0, 0, 24, 50),   # OI=74
    MockOpp(0, 50, 100, 24, 50), # No OI but vol+liq
    MockOpp(0, 0, 0, 24, 50),    # Nothing - should fail
]

print("Testing filter logic:\n")
for i, opp in enumerate(test_cases):
    result, reason = passes_filters(opp)
    status = "PASS" if result else "FAIL"
    print(f"Test {i+1}: OI={opp.open_interest}, Vol={opp.volume_24h}, Liq={opp.liquidity}")
    print(f"  -> {status}: {reason}\n")
