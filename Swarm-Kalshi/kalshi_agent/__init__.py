"""
Kalshi AI Trading Agent (v3 Swarm Edition)
==========================================

A fully autonomous trading agent for the Kalshi prediction-markets exchange.
The package is organised into the following modules:

Core (v2):
- **kalshi_client**     -- Authenticated REST client for the Kalshi API.
- **market_scanner**    -- Scans and filters active markets.
- **analysis_engine**   -- Scores trade opportunities by confidence.
- **human_behavior**    -- Simulates human-like interaction patterns.
- **risk_manager**      -- Enforces position-sizing and loss limits.
- **learning_engine**   -- Logs trades and recalibrates strategy.
- **dashboard**         -- Generates performance reports.
- **agent**             -- Main orchestrator loop.

Enhanced (v3):
- **backtester**        -- Pre-loads learning engine from settled markets.
- **external_signals**  -- Fetches external signals for fair-value estimates.
- **prior_knowledge**   -- Seeded Bayesian domain knowledge per specialist.
"""

__version__ = "3.0.0"
