"""Web search connector with pluggable backends.

Supported providers:
  - serpapi     (requires SERPAPI_KEY)
  - bing        (requires BING_API_KEY)
  - tavily      (requires TAVILY_API_KEY)
  - duckduckgo  (free, no API key required)

Includes domain whitelisting/blocking per the research agent spec.
Ported from Polymarket bot -- src/connectors/web_search.py.
"""

from __future__ import annotations

import abc
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, unquote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


# ── Data Models ──────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A single web search result."""
    title: str
    url: str
    snippet: str
    source: str = ""         # publisher / domain
    date: str = ""            # publication date if available
    position: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


# ── Domain filtering ────────────────────────────────────────────────

def is_domain_blocked(url: str, blocked: list[str]) -> bool:
    """Check if a URL's domain is on the blocked list."""
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(b.lower() in domain for b in blocked)


def score_domain_authority(url: str, primary: list[str], secondary: list[str]) -> float:
    """Score a URL's domain authority (0-1)."""
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return 0.3
    for p in primary:
        if p.lower() in domain:
            return 1.0
    for s in secondary:
        if s.lower() in domain:
            return 0.7
    if domain.endswith(".gov"):
        return 0.95
    if domain.endswith(".edu"):
        return 0.8
    return 0.4


# ── Abstract Provider ────────────────────────────────────────────────

class SearchProvider(abc.ABC):
    """Base class for web search providers."""

    @abc.abstractmethod
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        ...

    async def close(self) -> None:
        pass


# ── Gemini Grounded Search Provider ───────────────────────────────────

class GeminiSearchProvider(SearchProvider):
    """Google search grounding via Google Gemini 2.5 Flash."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gemini-2.5-flash",
    ):
        self._api_key = (
            api_key
            or os.environ.get("GEMINI_API_KEY", "")
            or os.environ.get("GOOGLE_API_KEY", "")
            or os.environ.get("GOOGLE_GENAI_API_KEY", "")
        ).strip()
        self._model = model
        self._client = None
        if not self._api_key:
            log.warning("GEMINI_API_KEY not set; Gemini search grounding will fail")

    def _get_client(self):
        if self._client is None and self._api_key:
            try:
                from google import genai
                self._client = genai.Client(api_key=self._api_key)
            except Exception as exc:
                log.warning("GeminiSearchProvider failed to init genai.Client: %s", exc)
        return self._client

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        client = self._get_client()
        if client is None:
            return []

        import asyncio
        try:
            from google.genai import types
        except ImportError:
            log.warning("google-genai package not installed")
            return []

        def _do_search():
            prompt = (
                f"Search the web for up-to-date and accurate factual information regarding: {query}\n\n"
                "Provide a clear, detailed factual summary with citations and source references."
            )
            return client.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1,
                ),
            )

        try:
            response = await asyncio.to_thread(_do_search)
        except Exception as exc:
            log.warning("Gemini search call failed: %s", exc)
            return []

        results: list[SearchResult] = []
        try:
            grounding_meta = None
            if response.candidates and len(response.candidates) > 0:
                grounding_meta = getattr(response.candidates[0], "grounding_metadata", None)

            full_text = (response.text or "").strip()
            chunks = getattr(grounding_meta, "grounding_chunks", None) or []

            seen_uris = set()
            for idx, chunk in enumerate(chunks):
                web = getattr(chunk, "web", None)
                if not web:
                    continue
                uri = getattr(web, "uri", "") or ""
                title = getattr(web, "title", "") or ""
                if not uri or uri in seen_uris:
                    continue
                seen_uris.add(uri)

                snippet = full_text[:400] if idx == 0 else title

                results.append(SearchResult(
                    url=uri,
                    title=title or f"Search result for {query}",
                    snippet=snippet,
                    source=title or (urlparse(uri).netloc if uri.startswith("http") else "gemini-search"),
                    position=idx + 1,
                    raw={"grounding_chunk": str(chunk)},
                ))
                if len(results) >= num_results:
                    break

            if not results and full_text:
                results.append(SearchResult(
                    url="https://www.google.com/search?q=" + query,
                    title=f"Gemini Grounded Search: {query[:60]}",
                    snippet=full_text[:400],
                    source="google-grounded-gemini",
                    position=1,
                ))

        except Exception as exc:
            log.warning("Gemini search extraction failed: %s", exc)
            return []

        log.info("gemini: query=%r results=%d", query[:80], len(results))
        return results


# ── SerpAPI Provider ─────────────────────────────────────────────────

class SerpAPIProvider(SearchProvider):
    """Google search via SerpAPI with automatic key rotation."""

    def __init__(self, api_key: str | None = None):
        raw = api_key or os.environ.get("SERPAPI_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("SERPAPI_KEY not set; searches will fail")
            self._keys = [""]
        self._key_index = 0
        self._client = httpx.AsyncClient(timeout=20.0)

    @property
    def _key(self) -> str:
        return self._keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Rotate to next key. Returns True if a new key is available."""
        next_idx = self._key_index + 1
        if next_idx < len(self._keys):
            self._key_index = next_idx
            log.info("serpapi: key rotated to index %d (total=%d)", next_idx, len(self._keys))
            return True
        return False

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        resp = await self._client.get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "api_key": self._key,
                "num": num_results,
                "engine": "google",
            },
        )
        # On rate limit, try rotating to next key before raising
        if resp.status_code == 429 and self._rotate_key():
            log.warning("serpapi: rate limited, retrying with rotated key")
            resp = await self._client.get(
                "https://serpapi.com/search.json",
                params={
                    "q": query,
                    "api_key": self._key,
                    "num": num_results,
                    "engine": "google",
                },
            )
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("organic_results", [])):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source=item.get("source", item.get("displayed_link", "")),
                    date=item.get("date", ""),
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("serpapi: query=%r results=%d", query[:80], len(results))
        return results


# ── Bing Provider ────────────────────────────────────────────────────

class BingProvider(SearchProvider):
    """Bing Web Search API v7."""

    def __init__(self, api_key: str | None = None):
        self._key = api_key or os.environ.get("BING_API_KEY", "")
        self._client = httpx.AsyncClient(timeout=20.0)

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        resp = await self._client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            headers={"Ocp-Apim-Subscription-Key": self._key},
            params={"q": query, "count": num_results, "mkt": "en-US"},
        )
        resp.raise_for_status()
        data = resp.json()
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("webPages", {}).get("value", [])):
            results.append(
                SearchResult(
                    title=item.get("name", ""),
                    url=item.get("url", ""),
                    snippet=item.get("snippet", ""),
                    source=item.get("displayUrl", ""),
                    date=item.get("dateLastCrawled", ""),
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("bing: query=%r results=%d", query[:80], len(results))
        return results


# ── Tavily Provider ──────────────────────────────────────────────────

class TavilyProvider(SearchProvider):
    """Tavily AI search API with automatic key rotation."""

    def __init__(self, api_key: str | None = None):
        raw = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            log.warning("TAVILY_API_KEY not set; searches will fail")
            self._keys = [""]
        self._key_index = 0
        self._client = httpx.AsyncClient(timeout=20.0)

    @property
    def _key(self) -> str:
        return self._keys[self._key_index]

    def _rotate_key(self) -> bool:
        """Rotate to next key. Returns True if a new key is available."""
        next_idx = self._key_index + 1
        if next_idx < len(self._keys):
            self._key_index = next_idx
            log.info("tavily: key rotated to index %d (total=%d)", next_idx, len(self._keys))
            return True
        return False

    async def close(self) -> None:
        await self._client.aclose()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        resp = await self._client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self._key,
                "query": query,
                "max_results": num_results,
                "search_depth": "advanced",
                "include_answer": False,
            },
        )
        # On rate limit or auth error, try rotating to next key before raising
        if resp.status_code in (429, 401, 403) and self._rotate_key():
            log.warning("tavily: rate limited, retrying with rotated key")
            resp = await self._client.post(
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
        results: list[SearchResult] = []
        for i, item in enumerate(data.get("results", [])):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source=(
                        item.get("url", "").split("/")[2]
                        if "/" in item.get("url", "") else ""
                    ),
                    date="",
                    position=i + 1,
                    raw=item,
                )
            )
        log.info("tavily: query=%r results=%d", query[:80], len(results))
        return results


# ── DuckDuckGo Provider ───────────────────────────────────────────────

class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo HTML scrape -- free fallback, no API key required."""

    def __init__(self):
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

    async def close(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        try:
            resp = await self._client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception as exc:
            log.warning("duckduckgo search failed: %s", exc)
            return []

        results: list[SearchResult] = []
        try:
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
                url = url_raw
                uddg_match = re.search(r"uddg=([^&]+)", url_raw)
                if uddg_match:
                    url = unquote(uddg_match.group(1))
                title = re.sub(r"<[^>]+>", "", title_raw).strip()
                snippet = ""
                if i < len(snippets_raw):
                    snippet = re.sub(r"<[^>]+>", "", snippets_raw[i]).strip()
                if not url or not url.startswith("http"):
                    continue
                results.append(SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    source=urlparse(url).netloc,
                    position=i + 1,
                ))
        except Exception as exc:
            log.warning("duckduckgo parse failed: %s", exc)
            return []

        log.info("duckduckgo: query=%r results=%d", query[:80], len(results))
        return results


# ── Factory ──────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[SearchProvider]] = {
    "gemini": GeminiSearchProvider,
    "serpapi": SerpAPIProvider,
    "bing": BingProvider,
    "tavily": TavilyProvider,
    "duckduckgo": DuckDuckGoProvider,
}


class FallbackSearchProvider(SearchProvider):
    """Search provider that tries multiple backends in order.

    If the primary provider fails (429, timeout, auth error), it
    automatically falls through to the next available provider.
    Default chain: gemini -> tavily -> serpapi -> duckduckgo.
    """

    def __init__(self, chain: list[str] | None = None):
        if chain is None:
            chain = ["gemini", "tavily", "serpapi", "duckduckgo"]
        self._chain: list[SearchProvider] = []
        for name in chain:
            cls = _PROVIDERS.get(name.lower())
            if cls:
                self._chain.append(cls())
        if not self._chain:
            self._chain.append(DuckDuckGoProvider())

    async def close(self) -> None:
        for provider in self._chain:
            await provider.close()

    async def search(self, query: str, num_results: int = 10) -> list[SearchResult]:
        last_error: Exception | None = None
        for i, provider in enumerate(self._chain):
            try:
                results = await provider.search(query, num_results)
                if results:
                    return results
            except Exception as e:
                provider_name = type(provider).__name__
                next_name = (
                    type(self._chain[i + 1]).__name__
                    if i + 1 < len(self._chain) else "none"
                )
                log.warning(
                    "search fallback: provider=%s error=%s next=%s",
                    provider_name, str(e), next_name,
                )
                last_error = e
                continue
        if last_error:
            log.error("search: all providers failed: %s", str(last_error))
        return []


def create_search_provider(name: str = "duckduckgo") -> SearchProvider:
    """Create a search provider by name.

    Use "fallback" for automatic fallback chain (tavily -> serpapi -> duckduckgo).
    """
    if name.lower() == "fallback":
        return FallbackSearchProvider()
    cls = _PROVIDERS.get(name.lower())
    if cls is None:
        log.warning(
            "Unknown search provider %r — falling back to duckduckgo. "
            "Choose from: %s",
            name, list(_PROVIDERS) + ["fallback"],
        )
        return DuckDuckGoProvider()
    return cls()
