#!/usr/bin/env python3
"""
run_swarm_with_openclaw_brain.py
================================

Main entry point that starts the Kalshi swarm with me (OpenClaw) as the controller.

This replaces run_swarm.py - instead of autonomous bots, they report to me.
"""

import sys
import os
import time
import signal
import logging
from pathlib import Path

# Ensure project root is on the path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("kalshi_swarm_openclaw")

def main():
    """Start the swarm with OpenClaw as the brain"""
    
    print("\n" + "="*70)
    print(" 🧠  KALSHI BOT SWARM v4 - OPENCLAW BRAIN MODE  🧠")
    print("="*70)
    print()
    print(" Configuration:")
    print("   • Controller: OpenClaw (Human-AI Hybrid)")
    print("   • Bots: 4 specialists (Sentinel, Oracle, Pulse, Vanguard)")
    print("   • Mode: Centralized decision-making")
    print("   • Dashboard: http://localhost:8080")
    print()
    print(" ⚠️  IMPORTANT:")
    print("   Bots will SCAN markets but WAIT for my approval to trade.")
    print("   I am now the trading brain.")
    print()
    print("="*70 + "\n")
    
    # Import and start the controller
    from openclaw_swarm_controller import get_controller
    
    controller = get_controller()
    
    # Get initial status
    summary = controller.get_swarm_summary()
    
    print(f"📊 INITIAL SWARM STATUS:")
    print(f"   Total Balance: {summary['total_balance']}")
    print(f"   Daily P&L: {summary['daily_pnl']}")
    print(f"   Bots Online: {summary['bots_online']}/4")
    print(f"   Open Positions: {summary['total_open_positions']}")
    print()
    
    # Check for critical issues
    if summary['bots_online'] < 4:
        print(f"⚠️  WARNING: Only {summary['bots_online']}/4 bots are online!")
        
    total_balance = sum(
        int(b['balance_cents']) 
        for b in summary['bot_details'].values()
    )
    
    if total_balance == 0:
        print("🚨 CRITICAL: All bots have $0 balance!")
        print("   Trading will be PAUSED until funds are added.")
        print()
    
    # Start the controller in background
    logger.info("Starting OpenClaw controller...")
    controller.start()
    
    # Now start the actual swarm coordinator and dashboard
    logger.info("Starting bot swarm coordinator with OpenClaw integration...")
    
    try:
        from run_swarm import setup_logging, ensure_directories, start_dashboard
        from swarm.swarm_coordinator import SwarmCoordinator
        
        setup_logging("INFO")
        ensure_directories()
        
        # Create the swarm coordinator (this spawns the 4 bots)
        coordinator = SwarmCoordinator(
            config_path="config/swarm_config.yaml",
            project_root=str(PROJECT_ROOT),
        )
        
        # Start dashboard WITH the coordinator
        dashboard_thread = start_dashboard(
            coordinator=coordinator,
            host="0.0.0.0",
            port=8080,
        )
        
        print("\n✅ SWARM STARTED SUCCESSFULLY")
        print(f"   Dashboard: http://localhost:8080")
        print(f"   Controller: OpenClaw (monitoring)")
        print(f"   Coordinator: Active (spawning 4 bots)")
        print()
        print("Commands:")
        print("   Ctrl+C - Stop swarm")
        print("   Check logs/swarm.log for bot activity")
        print()
        
        # Run the coordinator (blocks until shutdown, spawns all bots)
        coordinator.run()
            
    except KeyboardInterrupt:
        print("\n\n🛑 Shutting down swarm...")
        controller.stop()
        print("✅ Swarm stopped. Goodbye!")
        sys.exit(0)
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        controller.stop()
        sys.exit(1)

if __name__ == "__main__":
    main()
