"""
Map a Kalshi strike definition to the probability a forecast distribution
assigns to YES.

Kalshi markets carry ``strike_type`` with ``floor_strike`` / ``cap_strike``:

    greater            YES if value >  floor
    greater_or_equal   YES if value >= floor
    less               YES if value <  cap
    less_or_equal      YES if value <= cap
    between            YES if floor <= value <= cap

Integer-reported outcomes (temperatures) get a continuity correction.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

INF = float("inf")


def norm_cdf(x: float) -> float:
    if x == INF:
        return 1.0
    if x == -INF:
        return 0.0
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def yes_interval(strike_type: str, floor: Optional[float], cap: Optional[float],
                 integer_outcome: bool = False) -> Optional[Tuple[float, float]]:
    """Continuous interval (lo, hi) of outcomes that settle YES, or None if unknown."""
    st = (strike_type or "").lower()
    half = 0.5 if integer_outcome else 0.0
    if st == "greater" and floor is not None:
        return (floor + half, INF)
    if st == "greater_or_equal" and floor is not None:
        return (floor - half, INF)
    if st == "less" and cap is not None:
        return (-INF, cap - half)
    if st == "less_or_equal" and cap is not None:
        return (-INF, cap + half)
    if st == "between" and floor is not None and cap is not None:
        return (floor - half, cap + half)
    return None


def prob_normal(interval: Tuple[float, float], mu: float, sigma: float) -> float:
    lo, hi = interval
    sigma = max(float(sigma), 1e-9)
    return max(0.0, norm_cdf((hi - mu) / sigma) - norm_cdf((lo - mu) / sigma))


def prob_lognormal(interval: Tuple[float, float], spot: float, sigma_total: float) -> float:
    """Zero-drift lognormal: ln S_T ~ N(ln S0 - s^2/2, s^2) with s = sigma_total."""
    lo, hi = interval
    s = max(float(sigma_total), 1e-9)
    m = math.log(spot) - 0.5 * s * s

    def z(x: float) -> float:
        if x == INF:
            return INF
        if x <= 0:
            return -INF
        return (math.log(x) - m) / s

    return max(0.0, norm_cdf(z(hi)) - norm_cdf(z(lo)))
