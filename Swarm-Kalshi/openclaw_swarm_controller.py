"""
openclaw_swarm_controller.py
============================

Central brain controller that puts me in charge of all 4 Kalshi bots.
The bots become my sensors/actuators - they report to me, I decide.
"""

import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
import sqlite3
import threading

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("openclaw_controller")

@dataclass
class BotStatus:
    """Current status of a bot"""
    name: str
    specialist: str
    balance_cents: int
    open_positions: int
    daily_pnl_cents: int
    win_rate: float
    state: str
    last_update: str
    trade_recommendations: List[Dict] = None
    
    def __post_init__(self):
        if self.trade_recommendations is None:
            self.trade_recommendations = []

@dataclass
class TradeRequest:
    """A trade request from a bot"""
    bot_name: str
    ticker: str
    title: str
    category: str
    side: str  # 'yes' or 'no'
    quant_confidence: float
    market_context: Dict[str, Any]
    timestamp: str

class OpenClawSwarmController:
    """
    Central brain that manages the 4-bot Kalshi swarm.
    
    I am the controller - bots report to me, I make decisions.
    """
    
    def __init__(self, project_root: str = "D:\\kalshi-swarm-v4"):
        self.project_root = Path(project_root)
        self.bots: Dict[str, BotStatus] = {}
        self.pending_decisions: List[TradeRequest] = []
        self.controller_db = self.project_root / "data" / "openclaw_controller.db"
        self.command_queue = self.project_root / "data" / "controller_commands.jsonl"
        self.response_queue = self.project_root / "data" / "controller_responses.jsonl"
        self.running = False
        self.decision_thread = None
        
        # Initialize database
        self._init_db()
        
        # Bot definitions
        self.bot_configs = {
            "sentinel": {"specialist": "politics", "max_daily_trades": 5, "risk_level": "conservative"},
            "oracle": {"specialist": "economics", "max_daily_trades": 8, "risk_level": "moderate"},
            "pulse": {"specialist": "weather", "max_daily_trades": 6, "risk_level": "moderate"},
            "vanguard": {"specialist": "general", "max_daily_trades": 10, "risk_level": "balanced"}
        }
        
        logger.info("[CONTROLLER] OpenClaw Swarm Controller initialized")
        logger.info("[CONTROLLER] I am now the brain of the Kalshi swarm")
        
    def _init_db(self):
        """Initialize controller database"""
        self.controller_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.controller_db))
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                bot_name TEXT,
                ticker TEXT,
                side TEXT,
                quant_confidence REAL,
                my_confidence REAL,
                decision TEXT,
                rationale TEXT,
                executed BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                bot_name TEXT,
                command TEXT,
                parameters TEXT,
                executed BOOLEAN DEFAULT 0
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance (
                date TEXT PRIMARY KEY,
                total_trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                pnl_cents INTEGER,
                notes TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
        
    def read_bot_status(self, bot_name: str) -> Optional[BotStatus]:
        """Read current status from a bot's status file"""
        status_file = self.project_root / "data" / f"{bot_name}_status.json"
        
        if not status_file.exists():
            return None
            
        try:
            with open(status_file, 'r') as f:
                data = json.load(f)
                
            return BotStatus(
                name=data.get('bot_name', bot_name),
                specialist=data.get('specialist', 'unknown'),
                balance_cents=data.get('risk', {}).get('balance_cents', 0),
                open_positions=data.get('risk', {}).get('open_positions', 0),
                daily_pnl_cents=data.get('risk', {}).get('daily_pnl_cents', 0),
                win_rate=data.get('performance', {}).get('win_rate', 0),
                state=data.get('state', 'unknown'),
                last_update=data.get('timestamp', datetime.now(timezone.utc).isoformat())
            )
        except Exception as e:
            logger.error(f"[CONTROLLER] Error reading {bot_name} status: {e}")
            return None
            
    def update_all_bot_status(self):
        """Update status for all 4 bots"""
        for bot_name in self.bot_configs.keys():
            status = self.read_bot_status(bot_name)
            if status:
                self.bots[bot_name] = status
                logger.debug(f"[CONTROLLER] {bot_name}: ${status.balance_cents/100:.2f} | {status.state}")
                
    def analyze_trade(self, request: TradeRequest) -> Dict[str, Any]:
        """
        My brain analyzes a trade request and makes a decision.
        This is where I become the controller.
        """
        logger.info(f"[CONTROLLER] Analyzing trade: {request.bot_name} → {request.ticker} {request.side}")
        logger.info(f"[CONTROLLER] Quant confidence: {request.quant_confidence:.1f}%")
        
        # Get bot status
        bot_status = self.bots.get(request.bot_name)
        if not bot_status:
            return {"decision": "reject", "confidence": 0, "rationale": "Bot status unknown"}
            
        # My analysis logic
        my_confidence = request.quant_confidence
        rationale = []
        red_flags = []
        
        # Check bot's recent performance
        if bot_status.win_rate < 0.3 and bot_status.daily_pnl_cents < -500:
            my_confidence *= 0.5  # Reduce confidence for struggling bots
            red_flags.append(f"{request.bot_name} is underperforming ({bot_status.win_rate:.0%} win rate)")
            
        # Check balance
        if bot_status.balance_cents < 1000:
            red_flags.append(f"Low balance: ${bot_status.balance_cents/100:.2f}")
            
        # Check open positions
        if bot_status.open_positions >= 8:
            red_flags.append(f"High exposure: {bot_status.open_positions} positions open")
            my_confidence *= 0.8
            
        # Specialist-specific logic
        specialist = self.bot_configs[request.bot_name]["specialist"]
        
        if specialist == "politics":
            # Be very conservative with politics
            my_confidence *= 0.85
            rationale.append("Politics markets are highly volatile and sentiment-driven")
            
        elif specialist == "weather":
            # Weather has patterns but short-term uncertainty
            my_confidence *= 0.95
            rationale.append("Weather models have inherent uncertainty")
            
        elif specialist == "economics":
            # Economics moves slowly but predictably
            my_confidence *= 0.90
            rationale.append("Economic indicators lag - check Fed calendar")
            
        # Market context analysis
        market_context = request.market_context
        hours_to_expiry = market_context.get('hours_to_expiry', 720)
        
        if hours_to_expiry < 24:
            my_confidence *= 0.7  # Very short term = more risk
            red_flags.append("Market expires within 24 hours")
            
        # Confidence threshold
        min_confidence = 55 if specialist == "politics" else 50
        
        decision = "approve" if my_confidence >= min_confidence and len(red_flags) <= 2 else "reject"
        
        # Log my decision
        self._log_decision(request, my_confidence, decision, rationale, red_flags)
        
        result = {
            "decision": decision,
            "confidence": round(my_confidence, 2),
            "rationale": " | ".join(rationale) if rationale else "Standard analysis",
            "red_flags": red_flags,
            "adjusted_size": 1 if my_confidence > 70 else 0.5  # Position sizing
        }
        
        logger.info(f"[CONTROLLER] Decision: {decision.upper()} ({my_confidence:.1f}%)")
        if red_flags:
            logger.warning(f"[CONTROLLER] Red flags: {red_flags}")
            
        return result
        
    def _log_decision(self, request: TradeRequest, my_confidence: float, 
                      decision: str, rationale: List[str], red_flags: List[str]):
        """Log decision to database"""
        conn = sqlite3.connect(str(self.controller_db))
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO decisions 
            (timestamp, bot_name, ticker, side, quant_confidence, my_confidence, 
             decision, rationale)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            request.bot_name,
            request.ticker,
            request.side,
            request.quant_confidence,
            my_confidence,
            decision,
            json.dumps({"rationale": rationale, "red_flags": red_flags})
        ))
        
        conn.commit()
        conn.close()
        
    def send_command(self, bot_name: str, command: str, parameters: Dict = None):
        """Send a command to a specific bot"""
        cmd = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bot_name": bot_name,
            "command": command,
            "parameters": parameters or {}
        }
        
        with open(self.command_queue, 'a') as f:
            f.write(json.dumps(cmd) + "\n")
            
        logger.info(f"[CONTROLLER] Command sent to {bot_name}: {command}")
        
    def global_pause(self):
        """Pause all trading across all bots"""
        for bot_name in self.bot_configs.keys():
            self.send_command(bot_name, "pause_trading", {"reason": "Controller global pause"})
        logger.warning("[CONTROLLER] 🛑 GLOBAL PAUSE - All bots halted")
        
    def global_resume(self):
        """Resume all trading"""
        for bot_name in self.bot_configs.keys():
            self.send_command(bot_name, "resume_trading", {})
        logger.info("[CONTROLLER] ▶️ GLOBAL RESUME - All bots active")
        
    def adjust_risk(self, bot_name: str, risk_level: str):
        """Adjust risk level for a specific bot"""
        self.send_command(bot_name, "set_risk_level", {"level": risk_level})
        logger.info(f"[CONTROLLER] {bot_name} risk level set to {risk_level}")
        
    def get_swarm_summary(self) -> Dict:
        """Get summary of entire swarm"""
        self.update_all_bot_status()
        
        total_balance = sum(b.balance_cents for b in self.bots.values())
        total_positions = sum(b.open_positions for b in self.bots.values())
        total_daily_pnl = sum(b.daily_pnl_cents for b in self.bots.values())
        
        # Get today's decisions
        conn = sqlite3.connect(str(self.controller_db))
        cursor = conn.cursor()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        cursor.execute('''
            SELECT COUNT(*), SUM(CASE WHEN decision = 'approve' THEN 1 ELSE 0 END)
            FROM decisions WHERE date(timestamp) = ?
        ''', (today,))
        
        result = cursor.fetchone()
        total_decisions = result[0] or 0
        approved_decisions = result[1] or 0
        
        conn.close()
        
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_balance": f"${total_balance/100:.2f}",
            "total_open_positions": total_positions,
            "daily_pnl": f"${total_daily_pnl/100:.2f}",
            "bots_online": len([b for b in self.bots.values() if b.state == 'running']),
            "my_decisions_today": total_decisions,
            "approvals_today": approved_decisions,
            "bot_details": {name: asdict(status) for name, status in self.bots.items()}
        }
        
    def run_controller_loop(self):
        """Main control loop - I monitor and manage continuously"""
        self.running = True
        logger.info("[CONTROLLER] 🧠 Brain online - Managing swarm")
        
        while self.running:
            try:
                # Update bot statuses
                self.update_all_bot_status()
                
                # Check for any critical issues
                self._check_critical_conditions()
                
                # Print status every minute
                summary = self.get_swarm_summary()
                logger.info(f"[CONTROLLER] Swarm: {summary['bots_online']}/4 bots | "
                           f"Balance: {summary['total_balance']} | "
                           f"Positions: {summary['total_open_positions']}")
                
                # Sleep for a minute
                time.sleep(60)
                
            except Exception as e:
                logger.error(f"[CONTROLLER] Error in control loop: {e}")
                time.sleep(10)
                
    def _check_critical_conditions(self):
        """Check for conditions that need my intervention"""
        for bot_name, status in self.bots.items():
            # Check for massive losses
            if status.daily_pnl_cents < -1000:  # Lost $10+ today
                logger.error(f"[CONTROLLER] 🚨 {bot_name} lost ${abs(status.daily_pnl_cents)/100:.2f} today - PAUSING")
                self.send_command(bot_name, "pause_trading", {"reason": "Daily loss limit exceeded"})
                
            # Check for concerning win rate
            if status.win_rate < 0.2 and status.open_positions > 5:
                logger.warning(f"[CONTROLLER] ⚠️ {bot_name} win rate {status.win_rate:.0%} with {status.open_positions} positions")
                
    def stop(self):
        """Stop the controller"""
        self.running = False
        logger.info("[CONTROLLER] Stopping...")
        if self.decision_thread:
            self.decision_thread.join(timeout=5)
            
    def start(self):
        """Start the controller in a background thread"""
        self.decision_thread = threading.Thread(target=self.run_controller_loop, daemon=True)
        self.decision_thread.start()
        logger.info("[CONTROLLER] Controller started in background thread")


# Singleton instance
_controller = None

def get_controller() -> OpenClawSwarmController:
    """Get or create the singleton controller instance"""
    global _controller
    if _controller is None:
        _controller = OpenClawSwarmController()
    return _controller


if __name__ == "__main__":
    # Test the controller
    controller = get_controller()
    
    # Print initial status
    summary = controller.get_swarm_summary()
    print(f"\n{'='*60}")
    print(f"OPENCLAW SWARM CONTROLLER - STATUS REPORT")
    print(f"{'='*60}")
    print(f"Total Balance: {summary['total_balance']}")
    print(f"Daily P&L: {summary['daily_pnl']}")
    print(f"Bots Online: {summary['bots_online']}/4")
    print(f"My Decisions Today: {summary['my_decisions_today']}")
    print(f"Open Positions: {summary['total_open_positions']}")
    print(f"{'='*60}\n")
    
    # Start monitoring
    controller.start()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        controller.stop()
        print("\n[CONTROLLER] Shutdown complete")
