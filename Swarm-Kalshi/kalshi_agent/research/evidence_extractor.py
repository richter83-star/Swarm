"""Kalshi evidence extractor -- structured evidence from web sources via Claude.

Takes a list of SearchResult objects + a market question, calls Anthropic Claude
to extract structured evidence, then computes an independent quality score.

Key outputs (EvidencePackage):
  key_facts          -- list of factual strings extracted from sources
  supporting         -- evidence supporting YES resolution
  opposing           -- evidence supporting NO resolution
  confidence_assessment -- Claude's text assessment of confidence
  estimated_probability -- Claude's P(YES) estimate (0.0-1.0)
  quality_score      -- independent quality (0.0-1.0) based on:
                        recency (0.3) + authority (0.3) + agreement (0.2) + numeric_density (0.2)

Constraint: reuses the Anthropic client configured in llm_advisor.py indirectly
by reading the same ANTHROPIC_API_KEY env var and swarm config key.
Never creates a competing global client.

Usage::

    from kalshi_agent.research.evidence_extractor import KalshiEvidenceExtractor
    from kalshi_agent.research.web_search import SearchResult

    extractor = KalshiEvidenceExtractor(config={"extraction_model": "claude-haiku-4-5-20251001"})
    package = await extractor.extract(
        market_question="Will the Fed cut rates in December 2025?",
        sources=search_results,
        category="ECONOMICS",
    )
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from kalshi_agent.research.web_search import SearchResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class EvidencePackage:
    """Structured evidence extracted from web sources for a Kalshi market."""

    market_question: str
    category: str = "OTHER"

    # LLM-extracted fields
    key_facts: List[str] = field(default_factory=list)
    supporting_evidence: List[str] = field(default_factory=list)   # evidence for YES
    opposing_evidence: List[str] = field(default_factory=list)     # evidence for NO
    confidence_assessment: str = ""
    estimated_probability: float = 0.5   # LLM's P(YES) estimate

    # Independent quality scoring
    quality_score: float = 0.0           # final blended quality (0.0-1.0)
    recency_score: float = 0.0
    authority_score: float = 0.0
    agreement_score: float = 0.0
    numeric_density_score: float = 0.0

    # Metadata
    num_sources: int = 0
    reasoning: str = ""                  # LLM reasoning text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market_question": self.market_question,
            "category": self.category,
            "key_facts": self.key_facts,
            "supporting_evidence": self.supporting_evidence,
            "opposing_evidence": self.opposing_evidence,
            "confidence_assessment": self.confidence_assessment,
            "estimated_probability": self.estimated_probability,
            "quality_score": self.quality_score,
            "recency_score": self.recency_score,
            "authority_score": self.authority_score,
            "agreement_score": self.agreement_score,
            "numeric_density_score": self.numeric_density_score,
            "num_sources": self.num_sources,
            "reasoning": self.reasoning,
        }

    def as_rationale_text(self) -> str:
        """Format evidence as a short rationale string for trade records."""
        lines = []
        if self.supporting_evidence:
            lines.append(f"FOR: {'; '.join(self.supporting_evidence[:2])}")
        if self.opposing_evidence:
            lines.append(f"AGAINST: {'; '.join(self.opposing_evidence[:2])}")
        if self.confidence_assessment:
            lines.append(f"Assessment: {self.confidence_assessment}")
        lines.append(
            f"Evidence quality={self.quality_score:.2f} "
            f"P(YES)={self.estimated_probability:.2f} "
            f"sources={self.num_sources}"
        )
        return " | ".join(lines)


# ---------------------------------------------------------------------------
# Independent quality scoring
# ---------------------------------------------------------------------------

def _compute_quality_score(
    sources: List[SearchResult],
    has_numeric_facts: bool,
    has_contradictions: bool,
) -> Dict[str, float]:
    """Compute evidence quality independently of LLM self-assessment.

    Dimensions:
      recency_score    (0.3 weight): are sources recent (have dates)?
      authority_score  (0.3 weight): avg authority of sources
      agreement_score  (0.2 weight): 1.0 if no contradictions, lower if contradictory
      numeric_density  (0.2 weight): presence of numbers/statistics

    Returns dict with all sub-scores and 'final' quality score.
    """
    if not sources:
        return {
            "recency": 0.4, "authority": 0.3,
            "agreement": 1.0, "numeric_density": 0.2,
            "final": 0.32,
        }

    now = dt.datetime.now(dt.timezone.utc)

    # --- Recency ---
    recency_scores = []
    for src in sources:
        if src.date:
            try:
                date_str = src.date.strip()
                src_date = None
                for fmt in ["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%b %d, %Y", "%B %d, %Y"]:
                    try:
                        src_date = dt.datetime.strptime(date_str[:19], fmt)
                        src_date = src_date.replace(tzinfo=dt.timezone.utc)
                        break
                    except ValueError:
                        continue
                if src_date:
                    age_days = (now - src_date).days
                    if age_days <= 7:
                        recency_scores.append(1.0)
                    elif age_days <= 30:
                        recency_scores.append(0.6)
                    else:
                        recency_scores.append(0.2)
                else:
                    recency_scores.append(0.4)
            except Exception:
                recency_scores.append(0.4)
        else:
            recency_scores.append(0.4)
    recency = sum(recency_scores) / len(recency_scores) if recency_scores else 0.4

    # --- Authority ---
    auth_scores = [s.authority_score for s in sources if s.authority_score > 0]
    authority = sum(auth_scores) / len(auth_scores) if auth_scores else 0.3
    has_gov = any(s.authority_score >= 0.95 for s in sources)
    if has_gov:
        authority = min(1.0, authority + 0.10)

    # --- Agreement ---
    agreement = 0.7 if has_contradictions else 1.0

    # --- Numeric density ---
    numeric_density = 0.7 if has_numeric_facts else 0.3

    # Weighted final
    final = (
        recency * 0.30
        + authority * 0.30
        + agreement * 0.20
        + numeric_density * 0.20
    )

    return {
        "recency": round(recency, 3),
        "authority": round(authority, 3),
        "agreement": round(agreement, 3),
        "numeric_density": round(numeric_density, 3),
        "final": round(final, 3),
    }


# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are a precise research analyst extracting evidence for a Kalshi prediction market.

MARKET QUESTION: {question}
MARKET CATEGORY: {category}

SOURCES:
{sources_block}

TASK:
Analyze all sources and extract structured evidence. Return valid JSON only:
{{
  "key_facts": [
    "Specific fact with numbers/dates from source (e.g. 'CPI was 3.2% YoY in January 2026')"
  ],
  "supporting_evidence": [
    "Evidence that supports YES resolution (1-4 bullets)"
  ],
  "opposing_evidence": [
    "Evidence that opposes YES resolution / supports NO (1-4 bullets)"
  ],
  "has_numeric_facts": <bool>,
  "has_contradictions": <bool>,
  "confidence_assessment": "Brief 1-2 sentence assessment of overall evidence confidence",
  "estimated_probability": <float 0.0-1.0>,
  "reasoning": "2-3 sentence explanation of your probability estimate"
}}

RULES:
- Extract ONLY facts found in the sources above. Never fabricate data.
- estimated_probability: your best P(YES) estimate based on the evidence.
- If sources contradict, set has_contradictions=true and note both sides.
- If no relevant evidence found: estimated_probability=0.5, confidence_assessment="Insufficient evidence".
- key_facts should include specific numbers, percentages, dates when available.
- supporting/opposing evidence should each be concise bullet strings (max 30 words each).

Return ONLY valid JSON, no markdown fences, no extra text.
"""


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class KalshiEvidenceExtractor:
    """Extract structured evidence from search results using Anthropic Claude.

    Uses the same ANTHROPIC_API_KEY environment variable as LLMAdvisor.
    Does NOT create a persistent global client -- instantiates one per extract() call
    to avoid cross-contamination.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self._config = config or {}
        self._model = str(
            self._config.get("extraction_model", "claude-haiku-4-5-20251001")
        )
        self._max_tokens = int(self._config.get("llm_max_tokens", 1500))
        # API key: config takes precedence, then env var
        self._api_key = (
            self._config.get("extraction_api_key")
            or self._config.get("anthropic_api_key")
            or None  # Will use ANTHROPIC_API_KEY env var in anthropic library
        )

    async def extract(
        self,
        market_question: str,
        sources: List[SearchResult],
        category: str = "OTHER",
    ) -> EvidencePackage:
        """Extract evidence from search results.

        Args:
            market_question: The market question to research.
            sources: List of SearchResult objects from web search.
            category: Kalshi market category (e.g. "ECONOMICS").

        Returns:
            EvidencePackage with structured evidence. Never raises -- returns
            a minimal package with quality_score=0.0 on any failure.
        """
        if not sources:
            log.info("[research] evidence_extractor: no sources for question=%r", market_question[:60])
            return EvidencePackage(
                market_question=market_question,
                category=category,
                num_sources=0,
                quality_score=0.0,
                reasoning="No sources available for analysis.",
            )

        # Build sources block
        source_lines: List[str] = []
        for i, s in enumerate(sources):
            content_text = s.full_content[:2000] if s.full_content else s.snippet[:400]
            source_lines.append(
                f"[{i}] {s.title}\n"
                f"    URL: {s.url}\n"
                f"    Authority: {s.authority_score:.2f}\n"
                f"    Date: {s.date or 'unknown'}\n"
                f"    Content: {content_text}"
            )
        sources_block = "\n\n".join(source_lines)

        prompt = _EXTRACTION_PROMPT.format(
            question=market_question,
            category=category,
            sources_block=sources_block,
        )

        # Call Anthropic API
        try:
            raw_text = await self._call_anthropic(prompt)
        except Exception as exc:
            log.warning("[research] evidence_extractor: LLM call failed: %s", exc)
            return EvidencePackage(
                market_question=market_question,
                category=category,
                num_sources=len(sources),
                quality_score=0.0,
                reasoning=f"LLM extraction failed: {exc}",
            )

        # Parse JSON
        try:
            raw_text = raw_text.strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.split("\n", 1)[1] if "\n" in raw_text else raw_text[3:]
            if raw_text.endswith("```"):
                raw_text = raw_text[:-3]
            raw_text = raw_text.strip()
            parsed = json.loads(raw_text)
        except Exception as exc:
            log.warning("[research] evidence_extractor: JSON parse failed: %s | raw=%r", exc, raw_text[:200])
            return EvidencePackage(
                market_question=market_question,
                category=category,
                num_sources=len(sources),
                quality_score=0.0,
                reasoning=f"JSON parse failed: {exc}",
            )

        # Compute independent quality score
        has_numeric = bool(parsed.get("has_numeric_facts", False))
        has_contradictions = bool(parsed.get("has_contradictions", False))
        quality_scores = _compute_quality_score(sources, has_numeric, has_contradictions)

        # LLM's own estimated probability (clamp to [0.05, 0.95])
        raw_prob = float(parsed.get("estimated_probability", 0.5))
        estimated_prob = max(0.05, min(0.95, raw_prob))

        package = EvidencePackage(
            market_question=market_question,
            category=category,
            key_facts=list(parsed.get("key_facts", [])),
            supporting_evidence=list(parsed.get("supporting_evidence", [])),
            opposing_evidence=list(parsed.get("opposing_evidence", [])),
            confidence_assessment=str(parsed.get("confidence_assessment", "")),
            estimated_probability=estimated_prob,
            quality_score=quality_scores["final"],
            recency_score=quality_scores["recency"],
            authority_score=quality_scores["authority"],
            agreement_score=quality_scores["agreement"],
            numeric_density_score=quality_scores["numeric_density"],
            num_sources=len(sources),
            reasoning=str(parsed.get("reasoning", "")),
        )

        log.info(
            "[research] evidence_extractor: question=%r sources=%d "
            "quality=%.3f P(YES)=%.3f key_facts=%d",
            market_question[:60], len(sources),
            package.quality_score, package.estimated_probability,
            len(package.key_facts),
        )
        return package

    async def _call_anthropic(self, prompt: str) -> str:
        """Call Anthropic API using the anthropic library."""
        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError(
                "anthropic library not installed. Run: pip install anthropic"
            )

        client = _anthropic.AsyncAnthropic(api_key=self._api_key)
        try:
            resp = await client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.1,
                system=(
                    "You are a precise research analyst for prediction markets. "
                    "Return only valid JSON. Never fabricate data."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for block in resp.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts) or "{}"
        finally:
            await client.close()
