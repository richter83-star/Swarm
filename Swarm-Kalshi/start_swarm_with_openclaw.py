#!/usr/bin/env python3
"""
start_swarm_with_openclaw.py
=============================

Starts the Kalshi swarm with OpenClaw as the LLM advisor.
Run this instead of run_swarm.py to use me as the learning source.
"""

import sys
import os
from pathlib import Path

# Ensure project root is on the path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Patch the LLM advisor BEFORE importing anything else
print("[INIT] Loading OpenClaw LLM Bridge...")
from openclaw_llm_bridge import patch_llm_advisor
patch_success = patch_llm_advisor()

if patch_success:
    print("[INIT] ✅ OpenClaw bridge active - bots will learn from me")
else:
    print("[INIT] ⚠️ OpenClaw bridge failed - falling back to standard mode")

# Now import and run the normal swarm startup
from run_swarm import main

if __name__ == "__main__":
    print("[INIT] Starting Kalshi Swarm v4 with OpenClaw integration...")
    print("[INIT] Dashboard: http://localhost:8080")
    print("[INIT] Press Ctrl+C to stop\n")
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n[INIT] Swarm shutdown requested")
        sys.exit(0)
