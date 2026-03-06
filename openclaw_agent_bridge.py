"""
openclaw_agent_bridge.py
========================

Bridge that connects the Kalshi agent to me (OpenClaw) as the controller.
Replaces the autonomous decision-making with my brain.
"""

import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
import sqlite3

logger = logging.getLogger("openclaw_bridge")

class OpenClawAgentBridge:
    """
    Bridge that makes the agent report to me (OpenClaw) for decisions.
    
    Instead of trading autonomously, the agent:
    1. Scans markets
    2. Identifies opportunities
    3. ASKS ME for approval
    4. Only trades if I say yes
    """
    
    def __init__(self, bot_name: str, project_root: str = "D:\\kalshi-swarm-v4"):
        self.bot_name = bot_name
        self.project_root = Path(project_root)
        self.controller_db = self.project_root / "data" / "openclaw_controller.db"
        self.my_requests_db = self.project_root / "data" / f"{bot_name}_requests.db"
        
        # Initialize my request database
        self._init_request_db()
        
        logger.info(f"[BRIDGE:{bot_name}] OpenClaw bridge initialized - I report to the controller")
        
    def _init_request_db(self):
        """Initialize database for my trade requests"""
        conn = sqlite3.connect(str(self.my_requests_db))
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                title TEXT,
                category TEXT,
                side TEXT,
                quant_confidence REAL,
                market_context TEXT,
                my_decision TEXT,
                controller_confidence REAL,
                executed BOOLEAN DEFAULT 0,
                execution_price_cents INTEGER,
                outcome TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        
    def request_trade_approval(self, ticker: str, title: str, category: str, 
                               side: str, quant_confidence: float,
                               market_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ask me (OpenClaw) for approval to trade.
        
        This is the critical hook - the bot identifies an opportunity
        but waits for MY decision before executing.
        """
        
        # Log the request
        request_id = self._log_request(ticker, title, category, side, 
                                       quant_confidence, market_context)
        
        logger.info(f"[BRIDGE:{self.bot_name}] Request #{request_id}: {ticker} {side} "
                   f"(quant confidence: {quant_confidence:.1f}%)")
        
        # Create the request object
        request = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_name": self.bot_name,
            "ticker": ticker,
            "title": title,
            "category": category,
            "side": side,
            "quant_confidence": quant_confidence,
            "market_context": market_context
        }
        
        # For now, use the embedded controller logic
        # In production, this would send to OpenClaw session and wait for response
        decision = self._ask_controller(request)
        
        # Update the request with decision
        self._update_request_with_decision(request_id, decision)
        
        return decision
        
    def _ask_controller(self, request: Dict) -> Dict[str, Any]:
        """
        Ask the controller (me) for a decision.
        
        This simulates what would happen if I were actively managing:
        1. Bot sends request
        2. I analyze it
        3. I return decision
        """
        
        # Import the controller
        try:
            from openclaw_swarm_controller import TradeRequest, get_controller
            
            controller = get_controller()
            
            # Convert to TradeRequest
            trade_req = TradeRequest(
                bot_name=request["bot_name"],
                ticker=request["ticker"],
                title=request["title"],
                category=request["category"],
                side=request["side"],
                quant_confidence=request["quant_confidence"],
                market_context=request["market_context"],
                timestamp=request["timestamp"]
            )
            
            # Get my decision
            decision = controller.analyze_trade(trade_req)
            
            return decision
            
        except Exception as e:
            logger.error(f"[BRIDGE:{self.bot_name}] Controller error: {e}")
            # Fail safe - reject if can't reach controller
            return {
                "decision": "reject",
                "confidence": 0,
                "rationale": f"Controller error: {e}",
                "red_flags": ["controller_unreachable"]
            }
            
    def _log_request(self, ticker: str, title: str, category: str,
                    side: str, quant_confidence: float, 
                    market_context: Dict) -> int:
        """Log trade request to database"""
        conn = sqlite3.connect(str(self.my_requests_db))
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO trade_requests 
            (timestamp, ticker, title, category, side, quant_confidence, market_context)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            ticker,
            title,
            category,
            side,
            quant_confidence,
            json.dumps(market_context)
        ))
        
        request_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return request_id
        
    def _update_request_with_decision(self, request_id: int, decision: Dict):
        """Update request with controller's decision"""
        conn = sqlite3.connect(str(self.my_requests_db))
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE trade_requests 
            SET my_decision = ?, controller_confidence = ?
            WHERE id = ?
        ''', (
            decision.get("decision", "reject"),
            decision.get("confidence", 0),
            request_id
        ))
        
        conn.commit()
        conn.close()
        
    def check_for_commands(self) -> List[Dict]:
        """Check if controller has sent me any commands"""
        command_queue = self.project_root / "data" / "controller_commands.jsonl"
        
        if not command_queue.exists():
            return []
            
        commands = []
        try:
            with open(command_queue, 'r') as f:
                for line in f:
                    cmd = json.loads(line.strip())
                    if cmd.get("bot_name") == self.bot_name:
                        commands.append(cmd)
                        
        except Exception as e:
            logger.error(f"[BRIDGE:{self.bot_name}] Error reading commands: {e}")
            
        return commands
        
    def get_performance_report(self) -> Dict:
        """Get my performance report for the controller"""
        conn = sqlite3.connect(str(self.my_requests_db))
        cursor = conn.cursor()
        
        # Total requests
        cursor.execute("SELECT COUNT(*) FROM trade_requests")
        total_requests = cursor.fetchone()[0]
        
        # Approved requests
        cursor.execute("SELECT COUNT(*) FROM trade_requests WHERE my_decision = 'approve'")
        approved = cursor.fetchone()[0]
        
        # Executed trades
        cursor.execute("SELECT COUNT(*) FROM trade_requests WHERE executed = 1")
        executed = cursor.fetchone()[0]
        
        # Win/loss
        cursor.execute("SELECT COUNT(*) FROM trade_requests WHERE outcome = 'win'")
        wins = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM trade_requests WHERE outcome = 'loss'")
        losses = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "bot_name": self.bot_name,
            "total_requests": total_requests,
            "approved": approved,
            "executed": executed,
            "wins": wins,
            "losses": losses,
            "approval_rate": approved / total_requests if total_requests > 0 else 0
        }


# Decorator to wrap trading decisions
def requires_openclaw_approval(func):
    """
    Decorator that ensures all trades get my approval first.
    
    Usage:
        @requires_openclaw_approval
        def execute_trade(self, ticker, side, ...):
            # This only runs if I approve
            pass
    """
    def wrapper(*args, **kwargs):
        # Extract trade parameters from args/kwargs
        # This is simplified - would need to match actual function signature
        logger.info("[DECORATOR] Trade requires OpenClaw approval")
        
        # In real implementation, would:
        # 1. Extract trade details
        # 2. Call bridge.request_trade_approval()
        # 3. Only execute if decision == "approve"
        
        return func(*args, **kwargs)
    
    return wrapper


if __name__ == "__main__":
    # Test the bridge
    bridge = OpenClawAgentBridge("sentinel")
    
    # Simulate a trade request
    test_request = {
        "ticker": "KXWHOWIN2024",
        "title": "Will Trump win the 2024 election?",
        "category": "politics",
        "side": "yes",
        "quant_confidence": 65.0,
        "market_context": {
            "mid_price": 45,
            "hours_to_expiry": 720,
            "volume_24h": 10000
        }
    }
    
    print("\n" + "="*60)
    print("TESTING OPENCLAW BRIDGE")
    print("="*60)
    
    decision = bridge.request_trade_approval(**test_request)
    
    print(f"\nTrade: {test_request['ticker']} {test_request['side']}")
    print(f"Quant Confidence: {test_request['quant_confidence']:.1f}%")
    print(f"My Decision: {decision['decision'].upper()}")
    print(f"My Confidence: {decision['confidence']:.1f}%")
    print(f"Rationale: {decision['rationale']}")
    if decision['red_flags']:
        print(f"Red Flags: {', '.join(decision['red_flags'])}")
    
    print("\n" + "="*60)
