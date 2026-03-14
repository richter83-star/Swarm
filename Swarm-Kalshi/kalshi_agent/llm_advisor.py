"""
llm_advisor.py
==============

Dedicated LLM advisor that provides a second-opinion confidence score
on high-potential trade signals before execution.

Uses the Anthropic Claude API directly (no external agents).  For each
signal above a configurable pre-screen threshold, the advisor constructs
a structured prompt with all available market context and asks Claude to:

  1. Assess the probability of YES resolution (0–100).
  2. Provide a brief rationale.
  3. Flag any red-flags that the quantitative model may have missed.

The LLM score is blended with the quantitative score using a configurable
weight (default: 20% LLM, 80% quant).  If the API is unavailable or
returns an error, the original score passes through unchanged.

All LLM calls are cached per ticker per scan cycle (same TTL as external
signals) to avoid duplicate API calls when multiple signals reference the
same market.

Configuration (under ``llm_advisor`` in config yaml)
-----------------------------------------------------
  enabled: true
  api_key: YOUR_ANTHROPIC_API_KEY      # or set ANTHROPIC_API_KEY env var
  model: claude-haiku-4-5-20251001     # cheapest model; override if desired
  max_tokens: 300
  temperature: 0.1                     # low temp for consistent scoring
  pre_screen_threshold: 60             # only call LLM if quant conf >= this
  llm_weight: 0.20                     # blend weight for LLM score
  cache_ttl_seconds: 300
  timeout_seconds: 10
  max_calls_per_cycle: 5               # hard cap to control API costs
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class LLMAdvisor:
    """
    Wraps the Anthropic Claude API to provide market-resolution
    probability estimates that augment the quantitative scoring engine.

    Parameters
    ----------
    config : dict
        The ``llm_advisor`` section of the bot config.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "enabled": True,
        "api_key": "",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "temperature": 0.1,
        "pre_screen_threshold": 60.0,
        "llm_weight": 0.20,
        "cache_ttl_seconds": 300,
        "timeout_seconds": 10,
        "max_calls_per_cycle": 5,
    }

    _SYSTEM_PROMPT = (
        "You are a prediction market analyst specialising in the Kalshi exchange. "
        "Your role is to assess the probability that a binary market resolves YES "
        "based on all available context. "
        "You are aware of the following market patterns: "
        "(1) ELECTABILITY PANIC: After a progressive candidate wins a primary, general "
        "election odds often drop sharply due to sentiment overreaction, then mean-revert "
        "as fundamentals reassert — fading this panic is historically profitable. "
        "(2) NARRATIVE VS FUNDAMENTALS: Political markets frequently misprice when driven "
        "by news cycles rather than base rates — look for markets where price moved sharply "
        "on narrative but underlying probability has not changed. "
        "(3) MENTION/APPEARANCE MARKETS: Markets requiring a specific person to mention a "
        "specific topic have low base rates and high uncertainty — discount heavily unless "
        "strong external evidence exists. "
        "(4) EXECUTIVE ACTION MARKETS: Markets on presidential actions (orders, trips, "
        "appointments) tend to resolve YES at higher rates than priced when the president "
        "has shown prior intent. "
        "(5) SHOCK ALPHA — MACRO MARKETS: Kalshi prediction market prices outperform "
        "institutional consensus forecasts by 40% lower MAE for CPI, jobs, GDP and Fed "
        "rate decisions. During shock events the advantage grows to 50-60%. When the "
        "market price deviates from consensus by >0.1pp, the market is right 75% of the "
        "time. Trust the market price over consensus — institutional forecasters herd near "
        "each other due to reputational risk while market participants face direct financial "
        "incentives for accuracy. "
        "(6) CONSENSUS HERDING: Institutional forecasters cluster near consensus even when "
        "private signals suggest divergence, because the career cost of being wrong alone "
        "exceeds the benefit of being right alone. This creates exploitable mispricings in "
        "economic data release markets — when the market disagrees with consensus, bet with "
        "the market. "
        "(7) MARKET-CONDITIONED PROMPTING: The current market price is a powerful prior. "
        "Academic research (arxiv:2602.21229) shows that incorporating market price as a "
        "crowd wisdom signal substantially improves LLM forecasting calibration for mention "
        "markets. Treat the market price as the aggregated forecast of all participants with "
        "real financial stakes — it should anchor your estimate unless you have strong "
        "specific evidence to the contrary. High-volume markets have stronger crowd signals. "
        "Respond ONLY with a JSON object containing exactly three keys: "
        "\"yes_probability\" (integer 0-100), "
        "\"rationale\" (one sentence, max 30 words), "
        "\"red_flags\" (list of up to 3 short strings, or empty list). "
        "Do not include any text outside the JSON object."
    )

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self._enabled = self.cfg.get("enabled", True)

        # Resolve API key: config > env var
        self._api_key: str = (
            self.cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if self._enabled and not self._api_key:
            logger.warning(
                "LLMAdvisor: no API key found. "
                "Set 'llm_advisor.api_key' in config or ANTHROPIC_API_KEY env var. "
                "LLM advice disabled."
            )
            self._enabled = False

        # Simple TTL cache: ticker -> (timestamp, result_dict)
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._calls_this_cycle: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def reset_cycle(self) -> None:
        """Call at the start of each scan cycle to reset the call counter."""
        self._calls_this_cycle = 0
        # Prune stale cache entries
        now = time.monotonic()
        ttl = self.cfg["cache_ttl_seconds"]
        self._cache = {
            k: v for k, v in self._cache.items() if now - v[0] < ttl
        }

    def adjust_confidence(
        self,
        ticker: str,
        title: str,
        category: str,
        side: str,
        quant_confidence: float,
        market_context: Optional[Dict[str, Any]] = None,
        external_signals: Optional[Dict[str, float]] = None,
    ) -> Tuple[float, Optional[str]]:
        """
        Return an adjusted confidence score blending quant + LLM estimates.

        Parameters
        ----------
        ticker : str
        title : str
        category : str
        side : str         "yes" or "no"
        quant_confidence : float  (0–100)
        market_context : dict, optional
            Extra market data: mid_price, hours_to_expiry, volume_24h, etc.
        external_signals : dict, optional
            Signals from ExternalSignals module.

        Returns
        -------
        (adjusted_confidence, llm_rationale_or_None)
        """
        if not self._enabled:
            return quant_confidence, None

        threshold = self.cfg["pre_screen_threshold"]
        if quant_confidence < threshold:
            return quant_confidence, None

        max_calls = self.cfg["max_calls_per_cycle"]
        if self._calls_this_cycle >= max_calls:
            logger.debug("LLMAdvisor: cycle call cap reached (%d). Skipping.", max_calls)
            return quant_confidence, None

        # Check cache
        cache_key = f"{ticker}:{side}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, result = cached
            if time.monotonic() - ts < self.cfg["cache_ttl_seconds"]:
                return self._blend(quant_confidence, result, side), result.get("rationale")

        # Call the API
        result = self._call_api(ticker, title, category, side, market_context, external_signals)
        if result is None:
            return quant_confidence, None

        self._cache[cache_key] = (time.monotonic(), result)
        self._calls_this_cycle += 1

        adjusted = self._blend(quant_confidence, result, side)
        rationale = result.get("rationale", "")
        red_flags = result.get("red_flags", [])

        if red_flags:
            logger.info(
                "LLMAdvisor [%s %s]: quant=%.1f → adj=%.1f | flags: %s",
                ticker, side, quant_confidence, adjusted, red_flags,
            )
        else:
            logger.debug(
                "LLMAdvisor [%s %s]: quant=%.1f → adj=%.1f | %s",
                ticker, side, quant_confidence, adjusted, rationale,
            )

        return adjusted, rationale

    # ------------------------------------------------------------------
    # Blending
    # ------------------------------------------------------------------

    def _blend(
        self, quant_confidence: float, llm_result: Dict[str, Any], side: str
    ) -> float:
        """
        Blend the quantitative confidence with the LLM probability estimate.

        The LLM returns P(YES).  If we're trading NO, we invert it.
        """
        llm_weight = float(self.cfg["llm_weight"])
        quant_weight = 1.0 - llm_weight

        yes_prob = float(llm_result.get("yes_probability", 50))
        # Convert LLM yes probability to a confidence-like score for the side
        if side == "yes":
            llm_conf = yes_prob
        else:
            llm_conf = 100.0 - yes_prob

        blended = quant_weight * quant_confidence + llm_weight * llm_conf
        return round(max(0.0, min(100.0, blended)), 2)

    # ------------------------------------------------------------------
    # API call
    # ------------------------------------------------------------------

    def _call_api(
        self,
        ticker: str,
        title: str,
        category: str,
        side: str,
        market_context: Optional[Dict[str, Any]],
        external_signals: Optional[Dict[str, float]],
    ) -> Optional[Dict[str, Any]]:
        """Call the Anthropic API and parse the JSON response."""
        try:
            import urllib.request
            import urllib.error

            prompt = self._build_prompt(
                ticker, title, category, side, market_context, external_signals
            )

            payload = json.dumps({
                "model": self.cfg["model"],
                "max_tokens": int(self.cfg["max_tokens"]),
                "temperature": float(self.cfg["temperature"]),
                "system": self._SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            )

            timeout = int(self.cfg["timeout_seconds"])
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            text = ""
            for block in body.get("content", []):
                if block.get("type") == "text":
                    text = block["text"].strip()
                    break

            # Strip possible markdown fences
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()

            result = json.loads(text)
            # Validate required keys
            if "yes_probability" not in result:
                raise ValueError("Missing yes_probability in LLM response")
            result["yes_probability"] = max(0, min(100, int(result["yes_probability"])))
            return result

        except Exception as exc:
            logger.debug("LLMAdvisor API call failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        ticker: str,
        title: str,
        category: str,
        side: str,
        market_context: Optional[Dict[str, Any]],
        external_signals: Optional[Dict[str, float]],
    ) -> str:
        ctx = market_context or {}
        sigs = external_signals or {}

        lines = [
            f"Market ticker: {ticker}",
            f"Title: {title}",
            f"Category: {category}",
            f"Trading side: {side.upper()}",
        ]

        if ctx:
            mid = ctx.get("mid_price")
            hours = ctx.get("hours_to_expiry")
            vol = ctx.get("volume_24h")
            oi = ctx.get("open_interest")
            if mid is not None:
                # Market-Conditioned Prompting (MCP): frame market price as crowd wisdom signal
                crowd_conviction = "strong" if vol and vol > 10000 else "moderate" if vol and vol > 1000 else "weak"
                lines.append(
                    f"Current market price: {mid}¢ (crowd wisdom estimate: {mid:.0f}% YES probability, "
                    f"{crowd_conviction} conviction based on volume)"
                )
            if hours is not None:
                lines.append(f"Hours until expiry: {hours:.1f}")
            if vol is not None:
                lines.append(f"24h volume: {vol} contracts")
            if oi is not None:
                lines.append(f"Open interest: {oi} contracts")

        if sigs:
            sig_parts = []
            for k, v in sigs.items():
                if v != 0.0:
                    direction = "bullish YES" if v > 0 else "bearish YES"
                    sig_parts.append(f"{k}={v:+.2f} ({direction})")
            if sig_parts:
                lines.append(f"External signals: {', '.join(sig_parts)}")

        # Add political pattern hints based on ticker/category
        political_categories = {"politics", "elections", "government", "congress", "executive", "legislative"}
        is_political = category.lower() in political_categories or any(
            ticker.upper().startswith(p) for p in ["KXELECT", "KXPRES", "KXLAGODAYS", "KXEOWEEK", "KXDHSFUNDING"]
        )
        if is_political:
            lines.append(
                "Note: This is a political market. Consider base rates, prior intent, "
                "and whether the current price reflects narrative panic or true fundamentals."
            )

        is_mention = "MENTION" in ticker.upper() or "mention" in title.lower()
        if is_mention:
            mid = ctx.get("mid_price")
            vol = ctx.get("volume_24h", 0)
            # Apply Market-Conditioned Prompting (MCP) from arxiv:2602.21229
            # Research shows framing market price as crowd forecast improves LLM calibration
            crowd_signal = (
                f" The market is currently pricing this at {mid:.0f}%, which represents "
                f"the aggregated crowd forecast from real-money participants."
            ) if mid is not None else ""
            high_vol_note = (
                " High volume indicates strong participant conviction in this price."
            ) if vol and vol > 5000 else ""
            lines.append(
                f"Note: This is a mention/appearance market — predicting whether specific "
                f"language will appear in future speech or text. Base rate for specific "
                f"topic mentions during specific events is historically low (10-30%) unless "
                f"strong contextual evidence exists.{crowd_signal}{high_vol_note} "
                f"Weight the market price heavily as it aggregates diverse participant "
                f"information about likely future language use."
            )

        # Economic data release markets — apply shock alpha knowledge
        macro_series = {"KXCPI", "KXJOB", "KXGDP", "KXFED", "KXPCE", "KXPPI", "KXUNR", "KXRETAIL"}
        is_macro = any(ticker.upper().startswith(s) for s in macro_series)
        if is_macro:
            lines.append(
                "Note: This is a macroeconomic data release market. Kalshi market prices "
                "outperform institutional consensus by 40-60% MAE. If the current market "
                "price deviates significantly from published consensus estimates, trust the "
                "market price — it is right 75% of the time when it disagrees with consensus."
            )

        lines.append("\nWhat is the probability (0-100) that this market resolves YES?")
        return "\n".join(lines)
