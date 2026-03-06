# API Authentication Fix Report

## Problem Identified
**All bots failing to authenticate with Kalshi API**

Error: `HTTP 401: INCORRECT_API_KEY_SIGNATURE`

This means the RSA signature generated for API requests is invalid.

## Why Debug Script Works But Bots Don't

### Debug Script
- Runs directly: `python debug_bot_balance.py`
- Working directory: `D:\kalshi-swarm-v4`
- Key path resolved correctly: `D:\kalshi-swarm-v4\keys\kalshi-private.key`
- Result: ✅ Authentication works, balance = $60.29

### Running Bots (via PM2)
- Started via: `pm2 start run_swarm.py`
- PM2 may change working directory or environment
- Key path might be resolving differently
- Result: ❌ Authentication fails, balance = $0

## Root Cause Hypothesis

1. **Working Directory Issue**: PM2 might be running from a different directory
2. **Path Resolution**: Relative path `keys/kalshi-private.key` might not resolve correctly
3. **Private Key Loading**: Key might fail to load silently and return None

## Evidence from Code

In `kalshi_client.py`:
```python
@staticmethod
def _load_private_key(path: str):
    key_path = Path(path)
    if not key_path.exists():
        logger.warning("Private key file not found at '%s'...", path)
        return None  # Returns None if key not found!
```

If key returns None, signing fails → Authentication error.

## Fix Strategy

### Option 1: Absolute Path in Config
Change `swarm_config.yaml`:
```yaml
api:
  private_key_path: D:/kalshi-swarm-v4/keys/kalshi-private.key
```

### Option 2: Fix Path Resolution in BotRunner
Ensure BotRunner always uses absolute paths from project_root.

### Option 3: Add Better Error Handling
Fail fast if key cannot be loaded (don't return None).

## Immediate Workaround

Restart swarm manually (not via PM2) to test:
```bash
cd D:\kalshi-swarm-v4
python run_swarm.py
```

This will use correct working directory.

## Verification Needed

1. Check PM2 working directory
2. Add debug logging to key loading
3. Test with absolute path

## Status

- API Credentials: ✅ Valid (proven by debug script)
- Balance Available: ✅ $60.29 confirmed
- Authentication: ❌ Failing in production
- Fix Required: Path resolution when running via PM2

---
**Next Step**: Apply absolute path fix and restart
