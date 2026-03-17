"""Kalshi research web search -- multi-provider search with TTL cache.

Provider chain (in order of preference):
  1. Tavily   (primary)     -- requires TAVILY_API_KEY env var
  2. SerpAPI  (fallback)    -- requires SERPAPI_KEY env var
  3. DuckDuckGo (last resort) -- free, no key needed

Authority scoring:
  .gov          -> 0.95
  .edu          -> 0.80
  Reuters / AP / Bloomberg -> 0.85
  CoinDesk / ESPN etc.     -> 0.70
  Unknown                  -> 0.40

Caching:
  Results are cached in memory per (query_text, provider) with a
  configurable TTL (default: 2 hours) to avoid re-searching the same query.

Usage::

    provider = create_kalshi_search_provider()
    results = await provider.search("Fed rate cut December 2025", num_results=5)
    for r in results:
        print(r.title, r.authority_score)
"""

from __future__ import annotations

import abc
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _load_env_file() -> None:
    """Load .env file (export KEY="value" format) into os.environ if keys not already set.
    Needed because the systemd service does not source the shell .env file."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    log.debug("[research] _load_env_file: looking for .env at %s (exists=%s)", env_path, env_path.exists())
    if not env_path.exists():
        log.warning("[research] _load_env_file: .env not found at %s — API keys won't load", env_path)
        return
    loaded: list[str] = []
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Handle: export KEY="value" or KEY="value" or KEY=value
            line = line.removeprefix("export").strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
                loaded.append(key)
        log.info("[research] _load_env_file: loaded keys from .env: %s", loaded if loaded else "(none new)")
    except Exception as exc:
        log.warning("[research] _load_env_file: failed to parse .env: %s", exc)


_load_env_file()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """A single web search result enriched with authority scoring."""
    url: str
    title: str
    snippet: str
    full_content: str = ""       # fetched page content (filled by SourceFetcher)
    authority_score: float = 0.0
    source: str = ""             # publisher / domain label
    date: str = ""
    position: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Authority scoring
# ---------------------------------------------------------------------------

# High-authority domains mapped to fixed scores (check before TLD rules)
_AUTHORITY_DOMAINS: Dict[str, float] = {
    "reuters.com":         0.85,
    "apnews.com":          0.85,
    "bloomberg.com":       0.85,
    "wsj.com":             0.82,
    "ft.com":              0.82,
    "nytimes.com":         0.78,
    "washingtonpost.com":  0.78,
    "economist.com":       0.80,
    "bbc.com":             0.78,
    "bbc.co.uk":           0.78,
    "cnbc.com":            0.72,
    "axios.com":           0.72,
    "politico.com":        0.73,
    "thehill.com":         0.68,
    "coindesk.com":        0.70,
    "cointelegraph.com":   0.65,
    "coingecko.com":       0.70,
    "espn.com":            0.70,
    "sports-reference.com":0.70,
    "baseball-reference.com":0.70,
    "basketball-reference.com":0.70,
    "techcrunch.com":      0.68,
    "theverge.com":        0.68,
    "arstechnica.com":     0.68,
    "statsnews.com":       0.68,
    "scotusblog.com":      0.80,
    "ballotpedia.org":     0.75,
    "fivethirtyeight.com": 0.78,
    "realclearpolitics.com": 0.70,
}


def score_authority(url: str) -> float:
    """Compute an authority score (0.0-1.0) for a URL.

    Rules (highest match wins):
    1. .gov TLD     -> 0.95
    2. .edu TLD     -> 0.80
    3. Known domain -> from _AUTHORITY_DOMAINS table
    4. Unknown      -> 0.40
    """
    try:
        netloc = urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return 0.40

    if netloc.endswith(".gov"):
        return 0.95
    if netloc.endswith(".edu"):
        return 0.80

    for domain, score in _AUTHORITY_DOMAINS.items():
        if domain in netloc:
            return score

    return 0.40


# ---------------------------------------------------------------------------
# TTL cache (shared across provider instances)
# ---------------------------------------------------------------------------

_CACHE_TTL_SECS = 7200  # 2 hours default

class _TTLCache:
    """Minimal in-memory TTL cache."""

    def __init__(self) -> None:
        self._store: Dict[str, tuple] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    def put(self, key: str, value: Any, ttl_secs: float = _CACHE_TTL_SECS) -> None:
        self._store[key] = (value, time.monotonic() + ttl_secs)

    def size(self) -> int:
        return len(self._store)


_search_cache = _TTLCache()


def _cache_key(provider_name: str, query: str) -> str:
    import hashlib
    return hashlib.sha256(f"{provider_name}|{query}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Abstract base provider
# ---------------------------------------------------------------------------

class SearchProvider(abc.ABC):
    """Base class for all search providers."""

    name: str = "base"

    @abc.abstractmethod
    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        ...

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tavily provider (primary)
# ---------------------------------------------------------------------------

class TavilyProvider(SearchProvider):
    """Tavily AI search API.

    Reads TAVILY_API_KEY from environment.
    """

    name = "tavily"

    def __init__(self, api_key: Optional[str] = None, ttl_secs: float = _CACHE_TTL_SECS):
        raw = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("[research] TavilyProvider: TAVILY_API_KEY not set")
            self._keys = [""]
        self._key_idx = 0
        self._ttl = ttl_secs
        self._client = None

    @property
    def _key(self) -> str:
        return self._keys[self._key_idx]

    def _rotate_key(self) -> bool:
        nxt = self._key_idx + 1
        if nxt < len(self._keys):
            self._key_idx = nxt
            return True
        return False

    async def _get_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        cache_key = _cache_key(self.name, query)
        cached = _search_cache.get(cache_key)
        if cached is not None:
            log.debug("[research] tavily cache hit: %r", query[:60])
            return cached

        client = await self._get_client()
        try:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._key,
                    "query": query,
                    "max_results": num_results,
                    "search_depth": "advanced",
                    "include_answer": False,
                },
            )
            if resp.status_code in (401, 403, 429) and self._rotate_key():
                log.warning("[research] tavily rate-limited; rotating key")
                resp = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": self._key,
                        "query": query,
                        "max_results": num_results,
                        "search_depth": "advanced",
                        "include_answer": False,
                    },
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("[research] tavily search failed: %s", exc)
            return []

        results: List[SearchResult] = []
        for i, item in enumerate(data.get("results", [])):
            url = item.get("url", "")
            results.append(SearchResult(
                url=url,
                title=item.get("title", ""),
                snippet=item.get("content", ""),
                authority_score=score_authority(url),
                source=url.split("/")[2] if "/" in url else "",
                date="",
                position=i + 1,
                raw=item,
            ))

        log.info("[research] tavily: query=%r results=%d", query[:60], len(results))
        _search_cache.put(cache_key, results, self._ttl)
        return results


# ---------------------------------------------------------------------------
# SerpAPI provider (fallback)
# ---------------------------------------------------------------------------

class SerpAPIProvider(SearchProvider):
    """Google search via SerpAPI.

    Reads SERPAPI_KEY from environment.
    """

    name = "serpapi"

    def __init__(self, api_key: Optional[str] = None, ttl_secs: float = _CACHE_TTL_SECS):
        raw = api_key or os.environ.get("SERPAPI_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("[research] SerpAPIProvider: SERPAPI_KEY not set")
            self._keys = [""]
        self._key_idx = 0
        self._ttl = ttl_secs
        self._client = None

    @property
    def _key(self) -> str:
        return self._keys[self._key_idx]

    def _rotate_key(self) -> bool:
        nxt = self._key_idx + 1
        if nxt < len(self._keys):
            self._key_idx = nxt
            return True
        return False

    async def _get_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        cache_key = _cache_key(self.name, query)
        cached = _search_cache.get(cache_key)
        if cached is not None:
            log.debug("[research] serpapi cache hit: %r", query[:60])
            return cached

        client = await self._get_client()
        try:
            resp = await client.get(
                "https://serpapi.com/search.json",
                params={"q": query, "api_key": self._key, "num": num_results, "engine": "google"},
            )
            if resp.status_code == 429 and self._rotate_key():
                log.warning("[research] serpapi rate-limited; rotating key")
                resp = await client.get(
                    "https://serpapi.com/search.json",
                    params={"q": query, "api_key": self._key, "num": num_results, "engine": "google"},
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.warning("[research] serpapi search failed: %s", exc)
            return []

        results: List[SearchResult] = []
        for i, item in enumerate(data.get("organic_results", [])):
            url = item.get("link", "")
            results.append(SearchResult(
                url=url,
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
                authority_score=score_authority(url),
                source=item.get("source", item.get("displayed_link", "")),
                date=item.get("date", ""),
                position=i + 1,
                raw=item,
            ))

        log.info("[research] serpapi: query=%r results=%d", query[:60], len(results))
        _search_cache.put(cache_key, results, self._ttl)
        return results


# ---------------------------------------------------------------------------
# DuckDuckGo provider (last resort, free, no key needed)
# ---------------------------------------------------------------------------

class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo HTML scrape -- free fallback, no API key required.

    Uses html.duckduckgo.com with a basic scraper.
    Results are less structured; authority scoring is applied normally.
    """

    name = "duckduckgo"

    def __init__(self, ttl_secs: float = _CACHE_TTL_SECS):
        self._ttl = ttl_secs
        self._client = None

    async def _get_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        cache_key = _cache_key(self.name, query)
        cached = _search_cache.get(cache_key)
        if cached is not None:
            log.debug("[research] duckduckgo cache hit: %r", query[:60])
            return cached

        client = await self._get_client()
        try:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            log.warning("[research] duckduckgo search failed: %s", exc)
            return []

        results: List[SearchResult] = []
        # Parse result links from DDG HTML response
        # DDG uses <a class="result__a" href="..."> for result titles
        # and <a class="result__snippet" ...> or <span class="result__snippet"> for snippets
        try:
            # Extract title + URL pairs
            link_pattern = re.compile(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            snippet_pattern = re.compile(
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                re.DOTALL,
            )

            links = link_pattern.findall(html)
            snippets_raw = snippet_pattern.findall(html)

            for i, (url_raw, title_raw) in enumerate(links[:num_results]):
                # DDG redirects through /l/?uddg=<encoded_url> -- decode it
                url = url_raw
                uddg_match = re.search(r"uddg=([^&]+)", url_raw)
                if uddg_match:
                    from urllib.parse import unquote
                    url = unquote(uddg_match.group(1))

                title = re.sub(r"<[^>]+>", "", title_raw).strip()
                snippet = ""
                if i < len(snippets_raw):
                    snippet = re.sub(r"<[^>]+>", "", snippets_raw[i]).strip()

                if not url or not url.startswith("http"):
                    continue

                results.append(SearchResult(
                    url=url,
                    title=title,
                    snippet=snippet,
                    authority_score=score_authority(url),
                    source=urlparse(url).netloc,
                    date="",
                    position=i + 1,
                ))
        except Exception as exc:
            log.warning("[research] duckduckgo parse failed: %s", exc)
            return []

        log.info("[research] duckduckgo: query=%r results=%d", query[:60], len(results))
        _search_cache.put(cache_key, results, self._ttl)
        return results


# ---------------------------------------------------------------------------
# Multi-provider fallback chain
# ---------------------------------------------------------------------------

class KalshiSearchProvider(SearchProvider):
    """Multi-provider fallback: Tavily -> SerpAPI -> DuckDuckGo.

    Tries each provider in order; falls back on any exception or empty result.
    Reads provider availability from config or environment.
    """

    name = "kalshi_search"

    def __init__(self, config: Optional[dict] = None, ttl_secs: float = _CACHE_TTL_SECS):
        cfg = config or {}
        providers_cfg = cfg.get("providers", {})

        self._chain: List[SearchProvider] = []

        # Tavily (primary)
        tavily_cfg = providers_cfg.get("tavily", {})
        if tavily_cfg.get("enabled", True):
            tavily_key = os.environ.get("TAVILY_API_KEY", "")
            if tavily_key:
                self._chain.append(TavilyProvider(api_key=tavily_key, ttl_secs=ttl_secs))
                log.info("[research] KalshiSearchProvider: Tavily enabled")
            else:
                log.info("[research] KalshiSearchProvider: Tavily skipped (no key)")

        # SerpAPI (fallback)
        serpapi_cfg = providers_cfg.get("serpapi", {})
        if serpapi_cfg.get("enabled", True):
            serpapi_key = os.environ.get("SERPAPI_KEY", "")
            if serpapi_key:
                self._chain.append(SerpAPIProvider(api_key=serpapi_key, ttl_secs=ttl_secs))
                log.info("[research] KalshiSearchProvider: SerpAPI enabled")
            else:
                log.info("[research] KalshiSearchProvider: SerpAPI skipped (no key)")

        # DuckDuckGo (last resort -- always available)
        ddg_cfg = providers_cfg.get("duckduckgo", {})
        if ddg_cfg.get("enabled", True):
            self._chain.append(DuckDuckGoProvider(ttl_secs=ttl_secs))
            log.info("[research] KalshiSearchProvider: DuckDuckGo enabled (fallback)")

        if not self._chain:
            # Always add DDG as guaranteed fallback
            self._chain.append(DuckDuckGoProvider(ttl_secs=ttl_secs))
            log.warning("[research] KalshiSearchProvider: no paid providers configured; using DDG only")

    async def close(self) -> None:
        for provider in self._chain:
            try:
                await provider.close()
            except Exception:
                pass

    async def search(self, query: str, num_results: int = 5) -> List[SearchResult]:
        last_error: Optional[Exception] = None
        for i, provider in enumerate(self._chain):
            try:
                results = await provider.search(query, num_results=num_results)
                if results:
                    return results
                # Empty result -- try next provider
                log.debug(
                    "[research] %s returned 0 results for query=%r; trying next",
                    provider.name, query[:60],
                )
            except Exception as exc:
                next_name = (
                    self._chain[i + 1].name if i + 1 < len(self._chain) else "none"
                )
                log.warning(
                    "[research] search fallback: provider=%s error=%s next=%s",
                    provider.name, exc, next_name,
                )
                last_error = exc
                continue

        if last_error:
            log.error("[research] all search providers failed: %s", last_error)
        return []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_kalshi_search_provider(
    config: Optional[dict] = None,
    ttl_secs: float = _CACHE_TTL_SECS,
) -> KalshiSearchProvider:
    """Create the multi-provider Kalshi search provider."""
    return KalshiSearchProvider(config=config, ttl_secs=ttl_secs)
