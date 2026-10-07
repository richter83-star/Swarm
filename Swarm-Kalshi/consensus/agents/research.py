"""
RESEARCH agent: an LLM forecaster with live web search.

One grounded Gemini call per market: it reads the market title, resolution
rules and close time, searches the web, and returns a probability.  The
market price is deliberately NOT shown, so the forecast stays independent
of the order book instead of anchoring to it.

Expensive tier: the shadow runner only calls it on markets where a cheap
agent already disagrees with the price, within a per-cycle budget.  Results
are cached per ticker.  Abstains if ``google-genai`` or ``GEMINI_API_KEY``
is missing, or the reply cannot be parsed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

from consensus.agents.base import Agent, Estimate, MarketContext

Forecaster = Callable[[str], str]

PROMPT = """You are a calibrated forecaster for a prediction market.

Market: {title}
Resolution rules: {rules}
Market closes (UTC): {close}
Current time (UTC): {now}

Search the web for the most recent relevant information, then estimate the
probability that this market resolves YES.  Be calibrated: use base rates,
avoid overconfidence, and say so when evidence is thin.

Reply with ONLY a JSON object, no prose before or after:
{{"p_yes": <number 0-1>, "confidence": <number 0-1>, "rationale": "<one sentence>"}}"""

_JSON_RE = re.compile(r"\{[^{}]*\"p_yes\"[^{}]*\}", re.S)


def parse_reply(text: str) -> Optional[Tuple[float, float, str]]:
    m = _JSON_RE.search(text or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        p = float(d["p_yes"])
        c = float(d.get("confidence", 0.5))
    except (ValueError, KeyError, TypeError):
        return None
    if not 0.0 <= p <= 1.0:
        return None
    return p, min(max(c, 0.0), 1.0), str(d.get("rationale", ""))


def gemini_forecaster(model: str, api_key: str) -> Forecaster:
    from google import genai
    from google.genai import types

    import time

    from consensus.data_sources import record_source

    client = genai.Client(api_key=api_key)

    def run(prompt: str) -> str:
        t0 = time.monotonic()
        ok = False
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.2,
                ),
            )
            ok = True
            return getattr(resp, "text", "") or ""
        finally:
            record_source("gemini /search", "generativelanguage.googleapis.com",
                          (time.monotonic() - t0) * 1000, ok)

    return run


class ResearchAgent(Agent):
    name = "research"
    family = "research"
    tier = "expensive"
    label = "RESEARCH"

    def __init__(self, config=None, forecaster: Optional[Forecaster] = None) -> None:
        super().__init__(config)
        self.cache_ttl_s = float(self.config.get("cache_ttl_s", 6 * 3600))
        self._cache: Dict[str, Tuple[float, Optional[Estimate]]] = {}
        self._forecaster = forecaster
        self._init_error = ""
        if self._forecaster is None and self.enabled:
            key = os.environ.get(self.config.get("api_key_env", "GEMINI_API_KEY"), "")
            if not key:
                self._init_error = "GEMINI_API_KEY not set"
            else:
                try:
                    self._forecaster = gemini_forecaster(self.config.get("model", "gemini-2.5-flash"), key)
                except Exception as exc:
                    self._init_error = f"gemini init failed: {exc}"

    def applies(self, ctx: MarketContext) -> bool:
        if self._forecaster is None:
            if self._init_error:
                raise RuntimeError(self._init_error)
            return False
        return bool(ctx.snapshot.title)

    def estimate(self, ctx: MarketContext) -> Optional[Estimate]:
        hit = self._cache.get(ctx.ticker)
        if hit and ctx.now - hit[0] < self.cache_ttl_s:
            return hit[1]
        close = ctx.raw.get("close_time") or "unknown"
        now_iso = datetime.fromtimestamp(ctx.now, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        prompt = PROMPT.format(title=ctx.snapshot.title, rules=(ctx.snapshot.rules_text or "")[:1500],
                               close=close, now=now_iso)
        parsed = parse_reply(self._forecaster(prompt))
        est = None
        if parsed:
            p, c, why = parsed
            est = Estimate(p_yes=p, confidence=c, rationale=why,
                           sources=(f"gemini:{self.config.get('model', 'gemini-2.5-flash')}+search",))
        self._cache[ctx.ticker] = (ctx.now, est)
        return est
