"""
analysis_engine.py
==================

Enhanced market opportunity scorer with:

* **Price momentum scoring** — detects whether a market is trending toward
  YES or NO resolution by analysing recent trade flow direction and velocity.
* **Category-aware edge adjustment** — applies a multiplier from the learning
  engine so the agent favours categories with historically strong win rates.
* **Calibrated confidence threshold** — uses the learning engine's observed
  calibration bias to dynamically raise or lower the entry bar.
* **Orderbook depth asymmetry** — a more nuanced fair-value estimate that
  weighs YES vs NO depth at multiple price levels, not just totals.
* **Momentum as a first-class scoring dimension** — price velocity and trade
  flow direction are tracked alongside edge/liquidity/volume/timing.
* **Research pipeline** — optional web research layer that enriches signals
  with evidence-based probability estimates from authoritative sources.

Scoring dimensions (sum to 1.0 via learned weights):
  edge · liquidity · volume · timing · momentum

Research integration (additive layer, never blocks trades):
  If research available AND quality >= 0.6  → evidence-based probability as
      primary signal; confidence ±15/±20 adjustment
  If research available AND quality < 0.6   → weak signal only (±5)
  If no research (low researchability)      → existing statistical analysis unchanged
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from kalshi_agent.market_scanner import MarketOpportunity

logger = logging.getLogger(__name__)


@dataclass
class ResearchResult:
    """Result of the research pipeline for a single market opportunity."""
    evidence_package: Any           # EvidencePackage (typed as Any to avoid import cycles)
    category: str = "OTHER"
    researchability_score: int = 0
    # Convenience accessors (populated from evidence_package)
    quality_score: float = 0.0
    estimated_probability: float = 0.5
    rationale_text: str = ""


@dataclass
class TradeSignal:
    """A scored trade recommendation produced by the analysis engine."""
    ticker: str
    event_ticker: str
    title: str
    category: str
    side: str          # "yes" or "no"
    action: str        # "buy"
    confidence: float  # 0–100
    suggested_price: int  # cents
    edge: float        # estimated edge in cents
    rationale: str

    # Sub-scores for diagnostics / learning
    edge_score: float = 0.0
    liquidity_score: float = 0.0
    volume_score: float = 0.0
    timing_score: float = 0.0
    momentum_score: float = 0.0
    volume_24h: int = 0
    spread_cents: int = 0

    # Research enrichment (optional -- populated when research pipeline ran)
    research_quality: float = 0.0
    research_probability: float = 0.0
    research_rationale: str = ""


class AnalysisEngine:
    """
    Scores market opportunities and emits TradeSignal objects.

    Parameters
    ----------
    config : dict
        The ``trading`` section of ``config.yaml``.
    weight_overrides : dict, optional
        Per-dimension weight overrides learned from past performance.
    learning_engine : LearningEngine, optional
        When provided, used to fetch category multipliers and calibrated
        confidence thresholds.
    external_signals : ExternalSignals, optional
        When provided, used to tilt fair-value estimates.
    llm_advisor : LLMAdvisor, optional
        When provided, used to blend LLM confidence into final scores.
    research_config : dict, optional
        The ``research`` section of ``swarm_config.yaml``. When provided and
        ``research.enabled: true``, activates the web research pipeline.
    """

    DEFAULT_WEIGHTS: Dict[str, float] = {
        "edge": 0.30,
        "liquidity": 0.22,
        "volume": 0.18,
        "timing": 0.18,
        "momentum": 0.12,
    }

    def __init__(
        self,
        config: Dict[str, Any],
        weight_overrides: Optional[Dict[str, float]] = None,
        learning_engine=None,
        external_signals=None,
        llm_advisor=None,
        research_config: Optional[Dict[str, Any]] = None,
    ):
        self.cfg = config
        self.learning = learning_engine
        self.external_signals = external_signals
        self.llm_advisor = llm_advisor
        self.weights = dict(self.DEFAULT_WEIGHTS)
        if weight_overrides:
            self.weights.update(weight_overrides)
        self._normalise_weights()

        # Research pipeline (optional, lazy-initialised on first use)
        self._research_cfg: Dict[str, Any] = research_config or {}
        self._research_enabled: bool = bool(self._research_cfg.get("enabled", False))
        self._research_min_score: int = int(
            self._research_cfg.get("min_researchability_score", 25)
        )
        self._research_timeout: float = float(
            self._research_cfg.get("search_timeout_seconds", 10)
        )
        self._research_min_quality: float = float(
            self._research_cfg.get("min_evidence_quality", 0.3)
        )
        # Lazy imports (avoid penalising startup when research is disabled)
        self._classifier = None
        self._query_builder = None
        self._search_provider = None
        self._extractor = None

    def _normalise_weights(self) -> None:
        total = sum(self.weights.values())
        if total > 0:
            self.weights = {k: v / total for k, v in self.weights.items()}

    # ------------------------------------------------------------------
    # Research pipeline helpers (lazy initialisation)
    # ------------------------------------------------------------------

    def _init_research(self) -> bool:
        """Lazy-initialise research pipeline components. Returns True if ready."""
        if not self._research_enabled:
            return False
        try:
            if self._classifier is None:
                from kalshi_agent.research.market_classifier import classify_kalshi_market
                self._classifier = classify_kalshi_market
            if self._query_builder is None:
                from kalshi_agent.research.query_builder import build_kalshi_queries
                self._query_builder = build_kalshi_queries
            if self._search_provider is None:
                from kalshi_agent.research.web_search import create_kalshi_search_provider
                self._search_provider = create_kalshi_search_provider(
                    config=self._research_cfg,
                    ttl_secs=float(self._research_cfg.get("cache_ttl_hours", 2)) * 3600,
                )
            if self._extractor is None:
                from kalshi_agent.research.evidence_extractor import KalshiEvidenceExtractor
                self._extractor = KalshiEvidenceExtractor(config=self._research_cfg)
            return True
        except Exception as exc:
            logger.warning("[research] Failed to initialise research pipeline: %s", exc)
            return False

    async def _run_research_async(self, opp: MarketOpportunity) -> Optional[ResearchResult]:
        """Execute the full research pipeline for one opportunity (async).

        Never raises -- returns None on any failure so the trade loop continues.
        """
        try:
            if not self._init_research():
                return None

            ticker = str(getattr(opp, "ticker", "") or "")
            title = str(getattr(opp, "title", "") or "")
            category_raw = str(getattr(opp, "category", "") or "")

            # 1. Classify
            classification = self._classifier(ticker=ticker, title=title)
            if classification.researchability_score < self._research_min_score:
                logger.info(
                    "[research] Skipping research for %s: researchability=%d < min=%d",
                    ticker, classification.researchability_score, self._research_min_score,
                )
                return None

            # 2. Build queries
            queries = self._query_builder(
                ticker=ticker,
                title=title,
                category=classification.category,
                researchability=classification.researchability_score,
                max_queries=classification.query_budget,
            )
            if not queries:
                return None

            # 3. Run web searches concurrently
            from kalshi_agent.research.web_search import SearchResult as KSR
            search_tasks = [
                self._search_provider.search(q.query_text, num_results=5)
                for q in queries
            ]
            raw_results = await asyncio.gather(*search_tasks, return_exceptions=True)

            # Flatten, de-duplicate by URL
            seen_urls: set = set()
            all_sources: List[KSR] = []
            for batch in raw_results:
                if isinstance(batch, BaseException):
                    logger.debug("[research] Search batch failed: %s", batch)
                    continue
                for sr in batch:
                    if sr.url not in seen_urls:
                        seen_urls.add(sr.url)
                        all_sources.append(sr)

            # Sort by authority
            all_sources.sort(key=lambda s: -s.authority_score)
            top_sources = all_sources[:8]

            if not top_sources:
                logger.info("[research] No sources found for %s", ticker)
                return None

            # 4. Extract evidence
            package = await self._extractor.extract(
                market_question=title,
                sources=top_sources,
                category=classification.category,
            )

            result = ResearchResult(
                evidence_package=package,
                category=classification.category,
                researchability_score=classification.researchability_score,
                quality_score=package.quality_score,
                estimated_probability=package.estimated_probability,
                rationale_text=package.as_rationale_text(),
            )

            logger.info(
                "[research] Completed research for %s: category=%s quality=%.3f P(YES)=%.3f",
                ticker, classification.category,
                package.quality_score, package.estimated_probability,
            )
            return result

        except Exception as exc:
            logger.warning("[research] Research pipeline error for %s: %s", getattr(opp, "ticker", "?"), exc)
            return None

    def research_market(self, opp: MarketOpportunity) -> Optional[ResearchResult]:
        """Synchronous wrapper for research pipeline with hard timeout.

        Runs the async pipeline in a new event loop with self._research_timeout
        timeout. Returns None on timeout, error, or disabled research.
        This method is non-blocking from the caller's perspective -- it either
        finishes within the timeout or returns None gracefully.
        """
        if not self._research_enabled:
            return None
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._run_research_async(opp),
                    timeout=self._research_timeout,
                )
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[research] Research timeout (%.0fs) for %s",
                self._research_timeout, getattr(opp, "ticker", "?"),
            )
            return None
        except Exception as exc:
            logger.warning(
                "[research] Research failed for %s: %s",
                getattr(opp, "ticker", "?"), exc,
            )
            return None

    def _apply_research_adjustment(
        self,
        confidence: float,
        side: str,
        research: ResearchResult,
        rationale: str,
    ) -> Tuple[float, str]:
        """Adjust confidence based on research evidence.

        Rules:
        - quality >= 0.6  (STRONG): evidence probability drives ±15/±20 adjustment
        - quality 0.3-0.6 (WEAK):   ±5 adjustment only
        - quality < 0.3   (POOR):   no adjustment
        - 'side' controls direction (yes/no inversion of estimated_probability)
        """
        quality = research.quality_score
        if quality < self._research_min_quality:
            return confidence, rationale

        # Evidence probability for the traded side
        ev_prob = research.estimated_probability  # P(YES) from evidence
        if side == "no":
            ev_prob = 1.0 - ev_prob

        # Our current confidence expressed as a probability (0-1)
        quant_prob = confidence / 100.0

        # Agreement/disagreement between quant signal and evidence
        agreement = ev_prob - quant_prob  # positive = evidence MORE bullish than quant

        research_note = (
            f"[research] cat={research.category} "
            f"quality={quality:.2f} P(YES)={research.estimated_probability:.2f}"
        )

        if quality >= 0.6:
            # Strong evidence -- allow up to ±15 boost or ±20 reduction
            if agreement > 0.15:
                # Evidence is significantly more bullish -- boost confidence
                boost = min(15.0, agreement * 50.0)
                confidence = min(100.0, confidence + boost)
                research_note += f" | evidence boosts +{boost:.1f}"
            elif agreement < -0.15:
                # Evidence is significantly more bearish -- reduce confidence
                reduction = min(20.0, abs(agreement) * 50.0)
                confidence = max(0.0, confidence - reduction)
                research_note += f" | evidence reduces -{reduction:.1f}"
            else:
                # Broadly agrees -- small boost for confirmation
                confidence = min(100.0, confidence + 3.0)
                research_note += " | evidence confirms"
        else:
            # Weak evidence (0.3-0.6) -- weak signal only ±5
            if agreement > 0.10:
                confidence = min(100.0, confidence + 5.0)
                research_note += " | weak evidence supports +5"
            elif agreement < -0.10:
                confidence = max(0.0, confidence - 5.0)
                research_note += " | weak evidence opposes -5"

        new_rationale = f"{rationale} | {research_note}"
        return confidence, new_rationale

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyse(self, opportunities: List[MarketOpportunity]) -> List[TradeSignal]:
        """
        Score every opportunity and return signals sorted by confidence.
        Uses calibrated threshold when a learning engine is attached.
        LLM advisor blends into confidence for high-scoring signals.
        Research pipeline enriches high-scoring signals with evidence.
        """
        base_threshold = self.cfg.get("min_confidence_threshold", 65)
        if self.learning is not None:
            threshold = self.learning.get_calibrated_threshold(base_threshold)
        else:
            threshold = base_threshold

        # Reset LLM call counter for the new cycle
        if self.llm_advisor is not None:
            self.llm_advisor.reset_cycle()

        # Research pre-filter: lower threshold so research can boost borderline signals
        research_trigger = float(self._research_cfg.get("trigger_threshold", 55))

        signals: List[TradeSignal] = []
        for opp in opportunities:
            signal = self._score(opp)
            if not signal:
                continue
            # Use lower threshold to allow research to boost borderline signals
            effective_min = min(threshold, research_trigger) if self._research_enabled else threshold
            if signal.confidence < effective_min:
                continue
            # LLM second opinion (only if already at full threshold)
            if signal.confidence >= threshold and self.llm_advisor is not None:
                ext_sigs = None
                if self.external_signals is not None:
                    try:
                        ext_sigs = self.external_signals.get_signals(
                            opp.ticker, opp.series_ticker, opp.category, opp.title
                        )
                    except Exception as exc:
                        logger.warning(
                            "External signals fetch failed for %s: %s",
                            opp.ticker, exc,
                        )
                adj_conf, llm_rationale = self.llm_advisor.adjust_confidence(
                    ticker=opp.ticker,
                    title=opp.title,
                    category=opp.category,
                    side=signal.side,
                    quant_confidence=signal.confidence,
                    market_context={
                        "mid_price": opp.mid_price,
                        "hours_to_expiry": opp.hours_to_expiry,
                        "volume_24h": opp.volume_24h,
                    },
                    external_signals=ext_sigs,
                )
                if adj_conf != signal.confidence:
                    signal.confidence = adj_conf
                    if llm_rationale:
                        signal.rationale += f" | LLM: {llm_rationale}"

            # Research pipeline enrichment — runs on ALL signals above research_trigger
            # so it can boost borderline signals to the final threshold
            if self._research_enabled:
                try:
                    research = self.research_market(opp)
                    if research is not None and research.quality_score >= self._research_min_quality:
                        adj_conf, adj_rationale = self._apply_research_adjustment(
                            confidence=signal.confidence,
                            side=signal.side,
                            research=research,
                            rationale=signal.rationale,
                        )
                        signal.confidence = adj_conf
                        signal.rationale = adj_rationale
                        signal.research_quality = research.quality_score
                        signal.research_probability = research.estimated_probability
                        signal.research_rationale = research.rationale_text
                        logger.info(
                            "[research] %s → quality=%.2f prob=%.2f conf=%d",
                            opp.ticker, research.quality_score,
                            research.estimated_probability, signal.confidence,
                        )
                except Exception as exc:
                    logger.warning(
                        "[research] Enrichment failed for %s: %s",
                        opp.ticker, exc,
                    )

            # Final threshold check after LLM + research adjustments
            if signal.confidence >= threshold:
                signals.append(signal)

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(
            "Analysis: %d signals above threshold %.1f from %d opportunities.",
            len(signals), threshold, len(opportunities),
        )
        return signals

    def update_weights(self, new_weights: Dict[str, float]) -> None:
        """Hot-reload scoring weights from the learning engine."""
        self.weights.update(new_weights)
        self._normalise_weights()
        logger.info("Scoring weights updated: %s", self.weights)

    # ------------------------------------------------------------------
    # Scoring logic
    # ------------------------------------------------------------------

    def _score(self, opp: MarketOpportunity) -> Optional[TradeSignal]:
        try:
            side, edge, suggested_price = self._estimate_edge(opp)
            if side is None:
                return None

            edge_sc  = self._edge_score(edge, opp)
            liq_sc   = self._liquidity_score(opp)
            vol_sc   = self._volume_score(opp)
            tim_sc   = self._timing_score(opp)
            mom_sc   = self._momentum_score(opp)

            raw = (
                self.weights.get("edge", 0.30) * edge_sc
                + self.weights.get("liquidity", 0.22) * liq_sc
                + self.weights.get("volume", 0.18) * vol_sc
                + self.weights.get("timing", 0.18) * tim_sc
                + self.weights.get("momentum", 0.12) * mom_sc
            )
            confidence = max(0.0, min(100.0, raw))

            # Apply category multiplier from learning engine.
            if self.learning is not None:
                cat_mult = self.learning.get_category_multiplier(opp.category)
                confidence = max(0.0, min(100.0, confidence * cat_mult))

            rationale = (
                f"Edge {edge:+.1f}¢ on {side.upper()} | "
                f"edge={edge_sc:.0f} liq={liq_sc:.0f} "
                f"vol={vol_sc:.0f} time={tim_sc:.0f} mom={mom_sc:.0f}"
            )

            return TradeSignal(
                ticker=opp.ticker,
                event_ticker=opp.event_ticker,
                title=opp.title,
                category=opp.category,
                side=side,
                action="buy",
                confidence=confidence,
                suggested_price=suggested_price,
                edge=edge,
                rationale=rationale,
                edge_score=edge_sc,
                liquidity_score=liq_sc,
                volume_score=vol_sc,
                timing_score=tim_sc,
                momentum_score=mom_sc,
                volume_24h=int(opp.volume_24h or 0),
                spread_cents=int(opp.spread or 0),
            )
        except Exception as exc:
            logger.debug("Scoring failed for %s: %s", opp.ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Sub-score functions (each returns 0–100)
    # ------------------------------------------------------------------

    def _estimate_edge(self, opp: MarketOpportunity) -> Tuple[Optional[str], float, int]:
        """
        Estimate fair value using multi-level orderbook depth asymmetry when
        available, with a mean-reversion fallback.
        """
        fair_value = self._fair_value(opp)

        yes_edge = fair_value - (opp.yes_ask if opp.yes_ask else opp.mid_price)
        no_ask_effective = opp.no_ask if opp.no_ask else (100 - opp.mid_price)
        no_edge = (100.0 - fair_value) - no_ask_effective

        buffer = self.cfg.get("limit_spread_buffer_cents", 1)

        if yes_edge > no_edge and yes_edge > 0:
            price = min(99, max(1, int(opp.yes_ask) + buffer)) if opp.yes_ask else int(fair_value)
            return "yes", yes_edge, price
        elif no_edge > 0:
            price = min(99, max(1, int(no_ask_effective) + buffer)) if opp.no_ask else int(100 - fair_value)
            return "no", no_edge, price

        # Both sides have negative edge — no profitable trade exists
        return None, 0, 0

    def _fair_value(self, opp: MarketOpportunity) -> float:
        """
        Derive a fair-value estimate for YES (0–100¢).

        Three-way blend:
          1. Orderbook depth asymmetry (when available)
          2. Momentum-adjusted mid-price fallback
          3. External signals tilt (news, consensus, resolution patterns)
        """
        # --- Base estimate from orderbook or mid-price ---
        if opp.orderbook:
            yes_levels = opp.orderbook.get("yes") or []
            no_levels = opp.orderbook.get("no") or []

            if yes_levels and no_levels:
                yes_depth = sum(q / (i + 1) for i, (_, q) in enumerate(yes_levels))
                no_depth = sum(q / (i + 1) for i, (_, q) in enumerate(no_levels))
                total = yes_depth + no_depth
                if total > 0:
                    imbalance = yes_depth / total
                    base_fv = opp.mid_price * 0.55 + (imbalance * 100) * 0.45
                else:
                    momentum_tilt = self._price_velocity(opp) * 0.5
                    base_fv = opp.mid_price + momentum_tilt
            else:
                momentum_tilt = self._price_velocity(opp) * 0.5
                base_fv = opp.mid_price + momentum_tilt
        else:
            momentum_tilt = self._price_velocity(opp) * 0.5
            base_fv = opp.mid_price + momentum_tilt

        # --- External signals tilt ---
        if self.external_signals is not None:
            try:
                sigs = self.external_signals.get_signals(
                    opp.ticker,
                    series_ticker=opp.series_ticker,
                    category=opp.category,
                    title=opp.title,
                )
                # Combine all non-zero signals into a single tilt (-1 to +1)
                total_sig = sum(sigs.values())
                n = sum(1 for v in sigs.values() if v != 0.0)
                if n > 0:
                    avg_sig = total_sig / n  # -1 to +1
                    # Apply a max 5-cent tilt from external signals
                    ext_tilt = avg_sig * 5.0
                    base_fv = base_fv + ext_tilt
            except Exception as exc:
                logger.debug("External signals unavailable for %s: %s", opp.ticker, exc)

        return max(1.0, min(99.0, base_fv))

    def _edge_score(self, edge: float, opp: MarketOpportunity) -> float:
        """Sigmoid mapping of percentage edge to 0–100."""
        price_ref = opp.mid_price if opp.mid_price > 0 else 50.0
        pct_edge = (edge / price_ref) * 100.0
        k = 0.3
        return 100.0 / (1.0 + math.exp(-k * pct_edge))

    def _liquidity_score(self, opp: MarketOpportunity) -> float:
        """Score based on spread tightness and orderbook depth."""
        if opp.spread <= 1:
            spread_pts = 60.0
        elif opp.spread <= 3:
            spread_pts = 50.0
        elif opp.spread <= 5:
            spread_pts = 35.0
        elif opp.spread <= 10:
            spread_pts = 20.0
        else:
            spread_pts = 5.0

        liq = opp.liquidity
        if liq >= 50000:
            depth_pts = 40.0
        elif liq >= 10000:
            depth_pts = 30.0
        elif liq >= 5000:
            depth_pts = 20.0
        elif liq >= 1000:
            depth_pts = 10.0
        else:
            depth_pts = 2.0

        return spread_pts + depth_pts

    def _volume_score(self, opp: MarketOpportunity) -> float:
        """Score based on 24-hour trading volume."""
        v = opp.volume_24h
        if v >= 5000:
            return 100.0
        elif v >= 1000:
            return 80.0
        elif v >= 500:
            return 60.0
        elif v >= 100:
            return 40.0
        elif v >= 50:
            return 25.0
        return 10.0

    def _timing_score(self, opp: MarketOpportunity) -> float:
        """Score based on hours until expiration (sweet spot: 6–48h)."""
        h = opp.hours_to_expiry
        if h <= 0:
            return 0.0
        if 6 <= h <= 48:
            return 100.0
        if 2 <= h < 6:
            return 70.0
        if 48 < h <= 168:
            return 60.0
        if 168 < h <= 720:
            return 30.0
        return 10.0

    def _momentum_score(self, opp: MarketOpportunity) -> float:
        """
        Score based on price momentum and trade flow direction.

        Positive momentum (price moving toward YES resolution) scores high.
        Negative momentum (price drifting toward 0 or 100 without flow) scores low.
        Neutral / no data scores at the midpoint.
        """
        velocity = self._price_velocity(opp)    # positive = YES trending
        flow = self._trade_flow_direction(opp)  # +1 YES buying, -1 NO buying, 0 neutral

        # Blend velocity and flow
        combined = (velocity * 0.6) + (flow * 40.0 * 0.4)  # normalise flow to ~velocity scale
        # Map to 0–100 via sigmoid
        k = 0.05
        score = 100.0 / (1.0 + math.exp(-k * combined))
        return score

    def _price_velocity(self, opp: MarketOpportunity) -> float:
        """
        Estimate price velocity from recent trades.
        Returns a value in approximately [-50, +50] cents-per-unit-time.
        Uses the market's recent_trades list if populated by the scanner.
        """
        trades = getattr(opp, "recent_trades", None)
        if not trades or len(trades) < 2:
            return 0.0
        # Compute slope from oldest to newest price
        prices = [t.get("yes_price", t.get("price", 50)) for t in trades[-5:]]
        if len(prices) < 2:
            return 0.0
        return prices[-1] - prices[0]

    def _trade_flow_direction(self, opp: MarketOpportunity) -> float:
        """
        Classify recent trade flow.
        Returns +1 (YES buying), -1 (NO buying), or 0 (mixed/unknown).
        """
        trades = getattr(opp, "recent_trades", None)
        if not trades:
            return 0.0
        yes_buys = sum(1 for t in trades if t.get("taker_side") == "yes")
        no_buys = sum(1 for t in trades if t.get("taker_side") == "no")
        total = yes_buys + no_buys
        if total == 0:
            return 0.0
        return (yes_buys - no_buys) / total  # -1 to +1
