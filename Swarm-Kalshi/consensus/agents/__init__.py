"""
WHALE-OS agent roster.

    BOOK      order-book microprice          family: microstructure
    WHALES    taker flow / large prints      family: microstructure
    MODEL     weather (Open-Meteo) or        family: weather_model
              crypto (Coinbase lognormal)    family: crypto_model
    HISTORY   series price calibration       family: history
    XVENUE    Polymarket price (mapped)      family: cross_venue
    RESEARCH  LLM + web search (budgeted)    family: research
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from consensus.agents.base import Agent, Estimate, MarketContext
from consensus.agents.crypto import CryptoAgent
from consensus.agents.cross_venue import CrossVenueAgent
from consensus.agents.flow import FlowAgent
from consensus.agents.history import HistoryAgent
from consensus.agents.orderbook import OrderbookAgent
from consensus.agents.research import ResearchAgent
from consensus.agents.weather import WeatherAgent

REGISTRY = {
    "book": OrderbookAgent,
    "whales": FlowAgent,
    "weather": WeatherAgent,
    "crypto": CryptoAgent,
    "history": HistoryAgent,
    "xvenue": CrossVenueAgent,
    "research": ResearchAgent,
}


def build_agents(config: Optional[Mapping[str, Any]]) -> List[Agent]:
    """Instantiate enabled agents from the ``agents:`` config section."""
    config = config or {}
    out: List[Agent] = []
    for name, cls in REGISTRY.items():
        cfg: Dict[str, Any] = dict(config.get(name) or {})
        if not cfg.get("enabled", name not in ("xvenue", "research")):
            continue
        cfg["enabled"] = True
        out.append(cls(cfg))
    return out


__all__ = ["Agent", "Estimate", "MarketContext", "REGISTRY", "build_agents",
           "OrderbookAgent", "FlowAgent", "WeatherAgent", "CryptoAgent",
           "HistoryAgent", "CrossVenueAgent", "ResearchAgent"]
