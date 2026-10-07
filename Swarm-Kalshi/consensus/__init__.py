"""
Consensus core for the Kalshi swarm.

Specialist agents each emit an ``AgentVote`` (a probability, or an
abstention).  The ``ConsensusEngine`` ("Jev") pools those votes with
reliability weights and a correlation discount, applies a deterministic
rules veto, and only fires when a quorum of independent evidence agrees
AND the net edge after Kalshi fees clears a threshold.

Nothing in this package places orders.  It produces ``Decision`` objects
that the bot runner (or a shadow-mode recorder) can act on or log.
"""

from consensus.schema import AgentVote, Decision, MarketSnapshot, VetoResult
from consensus.fees import (
    MAKER_RATE,
    TAKER_RATE,
    breakeven_probability,
    fee_per_contract_cents,
    trade_fee_cents,
)
from consensus.vetoes import VetoConfig, VetoRules
from consensus.reliability import ReliabilityStore
from consensus.aggregator import ConsensusConfig, ConsensusEngine
from consensus.ledger import DecisionLedger

__all__ = [
    "AgentVote",
    "Decision",
    "MarketSnapshot",
    "VetoResult",
    "MAKER_RATE",
    "TAKER_RATE",
    "breakeven_probability",
    "fee_per_contract_cents",
    "trade_fee_cents",
    "VetoConfig",
    "VetoRules",
    "ReliabilityStore",
    "ConsensusConfig",
    "ConsensusEngine",
    "DecisionLedger",
]
