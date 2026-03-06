"""
prior_knowledge.py
==================

Seeded domain knowledge for the Kalshi trading agent.  Provides:

1. **Category priors** -- Expected resolution rates and edge quality
   per market category (politics, economics, weather, etc.).
2. **Market-type resolution rates** -- Binary vs multi-bracket,
   above/below threshold, etc.
3. **Seeded scoring weights** -- Starting weight vectors tuned per
   specialist domain rather than the uniform defaults.
4. **Series-level edge priors** -- Known biases in specific recurring
   Kalshi series (e.g., CPI markets tend to resolve near consensus).

All priors are Bayesian: they are blended with observed data as it
accumulates.  The blending uses a configurable ``prior_strength``
parameter that controls how quickly observed data overwhelms the prior.

Usage
-----
The ``PriorKnowledge`` instance is consulted by the analysis engine
and learning engine to:
- Adjust initial confidence before any trades are logged.
- Provide starting category multipliers.
- Seed weight vectors for new bot instances.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =====================================================================
# Domain-specific prior databases
# =====================================================================

# Category priors: expected win rate, edge quality, volatility
# These are expert-seeded estimates based on Kalshi market patterns.
CATEGORY_PRIORS: Dict[str, Dict[str, float]] = {
    # Politics / Elections
    "politics": {
        "expected_win_rate": 0.52,
        "edge_quality": 0.6,
        "volatility": 0.7,
        "resolution_yes_rate": 0.45,
        "liquidity_factor": 1.2,
    },
    "elections": {
        "expected_win_rate": 0.50,
        "edge_quality": 0.5,
        "volatility": 0.8,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 1.5,
    },
    "government": {
        "expected_win_rate": 0.53,
        "edge_quality": 0.55,
        "volatility": 0.6,
        "resolution_yes_rate": 0.48,
        "liquidity_factor": 0.9,
    },
    "congress": {
        "expected_win_rate": 0.51,
        "edge_quality": 0.5,
        "volatility": 0.65,
        "resolution_yes_rate": 0.40,
        "liquidity_factor": 0.8,
    },
    # Economics / Finance
    "economics": {
        "expected_win_rate": 0.55,
        "edge_quality": 0.7,
        "volatility": 0.5,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 1.3,
    },
    "finance": {
        "expected_win_rate": 0.54,
        "edge_quality": 0.65,
        "volatility": 0.55,
        "resolution_yes_rate": 0.52,
        "liquidity_factor": 1.1,
    },
    "fed": {
        "expected_win_rate": 0.56,
        "edge_quality": 0.75,
        "volatility": 0.4,
        "resolution_yes_rate": 0.55,
        "liquidity_factor": 1.4,
    },
    # Climate / Weather / Science
    "climate": {
        "expected_win_rate": 0.54,
        "edge_quality": 0.65,
        "volatility": 0.5,
        "resolution_yes_rate": 0.55,
        "liquidity_factor": 0.7,
    },
    "weather": {
        "expected_win_rate": 0.53,
        "edge_quality": 0.6,
        "volatility": 0.6,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 0.6,
    },
    "science": {
        "expected_win_rate": 0.52,
        "edge_quality": 0.55,
        "volatility": 0.45,
        "resolution_yes_rate": 0.48,
        "liquidity_factor": 0.5,
    },
    # Culture / Tech / Crypto / Other
    "tech": {
        "expected_win_rate": 0.51,
        "edge_quality": 0.5,
        "volatility": 0.7,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 0.8,
    },
    "crypto": {
        "expected_win_rate": 0.50,
        "edge_quality": 0.45,
        "volatility": 0.9,
        "resolution_yes_rate": 0.48,
        "liquidity_factor": 1.0,
    },
    "entertainment": {
        "expected_win_rate": 0.50,
        "edge_quality": 0.4,
        "volatility": 0.75,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 0.6,
    },
    "sports": {
        "expected_win_rate": 0.51,
        "edge_quality": 0.45,
        "volatility": 0.8,
        "resolution_yes_rate": 0.50,
        "liquidity_factor": 0.9,
    },
}

# Series-level edge priors for known recurring Kalshi series
SERIES_PRIORS: Dict[str, Dict[str, float]] = {
    "KXCPI": {
        "yes_bias": 0.02,
        "edge_reliability": 0.7,
        "consensus_trackable": True,
        "mean_reversion_strength": 0.6,
    },
    "KXJOB": {
        "yes_bias": 0.0,
        "edge_reliability": 0.65,
        "consensus_trackable": True,
        "mean_reversion_strength": 0.5,
    },
    "KXGDP": {
        "yes_bias": 0.03,
        "edge_reliability": 0.6,
        "consensus_trackable": True,
        "mean_reversion_strength": 0.55,
    },
    "KXFED": {
        "yes_bias": 0.05,
        "edge_reliability": 0.8,
        "consensus_trackable": True,
        "mean_reversion_strength": 0.7,
    },
    "KXHIGHNY": {
        "yes_bias": -0.05,
        "edge_reliability": 0.5,
        "consensus_trackable": False,
        "mean_reversion_strength": 0.3,
    },
    "KXINX": {
        "yes_bias": 0.02,
        "edge_reliability": 0.55,
        "consensus_trackable": False,
        "mean_reversion_strength": 0.4,
    },
}

# Specialist weight presets
SPECIALIST_WEIGHTS: Dict[str, Dict[str, float]] = {
    "politics": {
        "edge": 0.25,
        "liquidity": 0.25,
        "volume": 0.20,
        "timing": 0.15,
        "momentum": 0.15,
    },
    "economics": {
        "edge": 0.35,
        "liquidity": 0.20,
        "volume": 0.15,
        "timing": 0.20,
        "momentum": 0.10,
    },
    "weather": {
        "edge": 0.30,
        "liquidity": 0.18,
        "volume": 0.15,
        "timing": 0.25,
        "momentum": 0.12,
    },
    "general": {
        "edge": 0.28,
        "liquidity": 0.22,
        "volume": 0.20,
        "timing": 0.15,
        "momentum": 0.15,
    },
}


class PriorKnowledge:
    """
    Bayesian prior knowledge manager.

    Blends seeded domain knowledge with observed data as it accumulates.

    Parameters
    ----------
    specialist : str
        The specialist domain: ``"politics"``, ``"economics"``,
        ``"weather"``, or ``"general"``.
    config : dict
        The ``prior_knowledge`` section of config.
    """

    def __init__(
        self,
        specialist: str = "general",
        config: Optional[Dict[str, Any]] = None,
    ):
        self.specialist = specialist
        self.cfg = config or {}
        self.prior_strength = self.cfg.get("prior_strength", 20)
        self._category_priors = dict(CATEGORY_PRIORS)
        self._series_priors = dict(SERIES_PRIORS)

        # Allow config to override/extend priors
        custom_cat = self.cfg.get("category_overrides", {})
        for cat, overrides in custom_cat.items():
            if cat in self._category_priors:
                self._category_priors[cat].update(overrides)
            else:
                self._category_priors[cat] = overrides

        custom_series = self.cfg.get("series_overrides", {})
        for series, overrides in custom_series.items():
            if series in self._series_priors:
                self._series_priors[series].update(overrides)
            else:
                self._series_priors[series] = overrides

    # ------------------------------------------------------------------
    # Bayesian blending
    # ------------------------------------------------------------------

    def blend_win_rate(
        self,
        category: str,
        observed_wins: int,
        observed_total: int,
    ) -> float:
        """
        Blend the prior expected win rate with observed data.

        Uses a Beta-Binomial conjugate model:
            posterior = (prior_wins + observed_wins) / (prior_total + observed_total)

        The prior_strength controls how many "virtual" observations the
        prior represents.
        """
        prior = self._get_category_prior(category)
        prior_wr = prior.get("expected_win_rate", 0.50)

        # Convert prior to virtual observations
        alpha_prior = prior_wr * self.prior_strength
        beta_prior = (1 - prior_wr) * self.prior_strength

        alpha_post = alpha_prior + observed_wins
        beta_post = beta_prior + (observed_total - observed_wins)

        return alpha_post / (alpha_post + beta_post)

    def blend_category_multiplier(
        self,
        category: str,
        observed_wins: int,
        observed_total: int,
        overall_win_rate: float,
    ) -> float:
        """
        Compute a Bayesian-blended category multiplier.

        Starts at the prior and converges to observed data as trades
        accumulate.
        """
        blended_wr = self.blend_win_rate(category, observed_wins, observed_total)
        multiplier = 1.0 + (blended_wr - max(0.01, overall_win_rate))
        return round(max(0.7, min(1.3, multiplier)), 3)

    # ------------------------------------------------------------------
    # Prior lookups
    # ------------------------------------------------------------------

    def get_category_prior(self, category: str) -> Dict[str, float]:
        """Return the full prior dict for a category."""
        return dict(self._get_category_prior(category))

    def get_series_prior(self, series_ticker: str) -> Dict[str, Any]:
        """Return the series-level prior, or defaults if unknown."""
        return dict(self._series_priors.get(series_ticker, {
            "yes_bias": 0.0,
            "edge_reliability": 0.5,
            "consensus_trackable": False,
            "mean_reversion_strength": 0.4,
        }))

    def get_initial_weights(self) -> Dict[str, float]:
        """Return the specialist-tuned starting weights."""
        weights = SPECIALIST_WEIGHTS.get(
            self.specialist,
            SPECIALIST_WEIGHTS["general"],
        )
        return dict(weights)

    def get_resolution_yes_prior(self, category: str) -> float:
        """Return the prior probability that a market resolves YES."""
        prior = self._get_category_prior(category)
        return prior.get("resolution_yes_rate", 0.50)

    def get_edge_quality(self, category: str) -> float:
        """
        Return the prior edge quality for a category (0-1).

        Higher values mean the category tends to have more exploitable
        mispricings.
        """
        prior = self._get_category_prior(category)
        return prior.get("edge_quality", 0.5)

    def get_fair_value_adjustment(
        self,
        series_ticker: str,
        mid_price: float,
    ) -> float:
        """
        Return a prior-based fair-value adjustment in cents.

        Uses series-level yes_bias and mean-reversion strength.
        """
        sp = self.get_series_prior(series_ticker)
        yes_bias = sp.get("yes_bias", 0.0)
        mr_strength = sp.get("mean_reversion_strength", 0.4)

        # Mean-reversion component: pull toward 50
        mr_adjustment = (50.0 - mid_price) * mr_strength * 0.1

        # Bias component
        bias_adjustment = yes_bias * 10.0  # scale to cents

        return round(mr_adjustment + bias_adjustment, 2)

    def get_confidence_floor(self, category: str) -> float:
        """
        Return a minimum confidence threshold for a category.

        Categories with lower edge quality get a higher floor (more
        conservative).
        """
        eq = self.get_edge_quality(category)
        # High edge quality (0.7+) -> floor of 55
        # Low edge quality (0.3)  -> floor of 75
        floor = 85.0 - (eq * 30.0)
        return round(max(50.0, min(80.0, floor)), 1)

    # ------------------------------------------------------------------
    # Specialist-specific filters
    # ------------------------------------------------------------------

    def get_category_filters(self) -> List[str]:
        """
        Return the list of categories this specialist should focus on.

        Returns an empty list for the 'general' specialist (no filter).
        """
        filters = {
            "politics": [
                "politics", "elections", "government", "congress",
                "executive", "legislative", "judicial", "regulation",
                "policy", "geopolitics", "international",
            ],
            "economics": [
                "economics", "finance", "fed", "inflation", "cpi",
                "jobs", "gdp", "interest_rates", "monetary_policy",
                "fiscal", "treasury", "bonds", "stocks",
            ],
            "weather": [
                "climate", "weather", "science", "environment",
                "temperature", "hurricane", "earthquake", "space",
                "energy", "natural_disaster",
            ],
            "general": [],  # No filter -- covers everything else
        }
        return filters.get(self.specialist, [])

    def get_series_filters(self) -> List[str]:
        """
        Return series tickers this specialist should prioritize.
        """
        filters = {
            "politics": [],
            "economics": [
                "KXCPI", "KXJOB", "KXGDP", "KXFED", "KXPCE",
                "KXUNR", "KXINFL", "KXPPI", "KXRETAIL", "KXHOUSING",
            ],
            "weather": [
                "KXHIGHNY", "KXLOWNY", "KXHURR", "KXTEMP",
            ],
            "general": [],
        }
        return filters.get(self.specialist, [])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_category_prior(self, category: str) -> Dict[str, float]:
        """Look up category prior, falling back to a neutral default."""
        cat_lower = (category or "").lower().strip()

        # Direct match
        if cat_lower in self._category_priors:
            return self._category_priors[cat_lower]

        # Partial match
        for key, prior in self._category_priors.items():
            if key in cat_lower or cat_lower in key:
                return prior

        # Default neutral prior
        return {
            "expected_win_rate": 0.50,
            "edge_quality": 0.5,
            "volatility": 0.6,
            "resolution_yes_rate": 0.50,
            "liquidity_factor": 1.0,
        }
