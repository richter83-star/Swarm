"""
Kalshi trading-fee math.

General schedule (verify against Kalshi's current fee schedule; maker
treatment can vary by series):

    taker fee = round_up(0.07   x C x P x (1 - P))   dollars, to the next cent
    maker fee = round_up(0.0175 x C x P x (1 - P))

where C = contracts and P = price in dollars.  The round-up applies to the
whole order, so very small orders pay a disproportionate fee (1 contract at
95c pays a full 1c on a 5c upside).
"""

from __future__ import annotations

import math

TAKER_RATE = 0.07
MAKER_RATE = 0.0175

_EPS = 1e-9


def _check_price(price_cents: int) -> None:
    if not 1 <= int(price_cents) <= 99:
        raise ValueError(f"price_cents must be in [1, 99], got {price_cents}")


def trade_fee_cents(contracts: int, price_cents: int, rate: float = TAKER_RATE) -> int:
    """Total fee in cents for one order, rounded up to the next cent."""
    contracts = int(contracts)
    if contracts <= 0:
        return 0
    _check_price(price_cents)
    if rate < 0:
        raise ValueError("rate must be non-negative")
    p = price_cents / 100.0
    raw_cents = rate * contracts * p * (1.0 - p) * 100.0
    return int(math.ceil(raw_cents - _EPS))


def fee_per_contract_cents(contracts: int, price_cents: int, rate: float = TAKER_RATE) -> float:
    """Average fee per contract (cents), including the order-level round-up."""
    contracts = int(contracts)
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    return trade_fee_cents(contracts, price_cents, rate) / contracts


def breakeven_probability(price_cents: int, fee_cents_per_contract: float) -> float:
    """Win probability needed to break even when buying at ``price_cents``."""
    _check_price(price_cents)
    return (price_cents + fee_cents_per_contract) / 100.0
