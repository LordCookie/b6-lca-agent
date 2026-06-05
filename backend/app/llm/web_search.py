"""Self-contained web search for the research agents (keyless, DuckDuckGo).

Replaces OpenRouter's ``openrouter:web_search`` server-tool, which many models
(e.g. openai/gpt-oss-120b) silently ignore — leaving ``web_search_requests=0``
and no citations. Here we run the search ourselves so the agents always get
REAL, citable result URLs (spec §10: no fabricated sources). The LLM then only
*extracts* facts FROM these real results and must cite one of the returned URLs.

Provider: DuckDuckGo via the ``ddgs`` package (formerly ``duckduckgo-search``).
No API key required → works out of the box inside Docker, good for the thesis'
reproducibility/handover. The provider is selected via ``settings`` so a keyed
backend (e.g. Tavily/Brave) can be added later without touching the agents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("lca.websearch")


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class WebSearchError(RuntimeError):
    """Raised when the search backend is unreachable after retries."""


def _ddgs_class():
    """Import DDGS from whichever package name is installed.

    The library was renamed ``duckduckgo-search`` → ``ddgs`` in 2025; support
    both so the pin can move without code changes.
    """
    try:
        from ddgs import DDGS  # type: ignore
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # type: ignore
        return DDGS


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type(Exception),
)
def _ddgs_text(query: str, max_results: int, region: str) -> list[dict[str, Any]]:
    DDGS = _ddgs_class()
    with DDGS() as ddgs:
        # ddgs.text yields dicts with keys: title, href, body
        return list(ddgs.text(query, region=region, max_results=max_results))


def web_search(
    query: str,
    *,
    max_results: int = 5,
    region: str = "de-de",
) -> list[SearchResult]:
    """Run a keyless web search and return normalised results.

    Never raises for an empty result set — callers treat ``[]`` as "nothing
    found" (spec §10: then no value is taken). Only a hard backend failure
    after retries raises :class:`WebSearchError`.
    """
    if not query or not query.strip():
        return []
    try:
        raw = _ddgs_text(query.strip(), max_results=max_results, region=region)
    except Exception as exc:  # network / rate-limit / parser issue
        log.warning("web_search failed for %r: %s", query, exc)
        raise WebSearchError(str(exc)) from exc

    out: list[SearchResult] = []
    for r in raw:
        url = r.get("href") or r.get("url") or ""
        if not url:
            continue
        out.append(
            SearchResult(
                title=str(r.get("title", "")).strip(),
                url=str(url).strip(),
                snippet=str(r.get("body") or r.get("snippet") or "").strip(),
            )
        )
    log.info("web_search %r → %d results", query, len(out))
    return out


__all__ = ["web_search", "SearchResult", "WebSearchError"]
