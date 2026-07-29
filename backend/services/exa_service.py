"""
Exa Search Service -- async web search via Exa API.

Used as:
1. A tool Claude can call in chat/agent/workflow/trigger (via tools/exa_tool.py)
2. Direct helper for background services (meeting prep, smart onboarding)

Exa returns clean text excerpts (not raw HTML), making it ideal for RAG.
"""

import os
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
EXA_BASE_URL = "https://api.exa.ai"


@dataclass
class ExaResult:
    title: str
    url: str
    excerpt: str
    published_date: str | None = None
    author: str | None = None
    summary: str | None = None
    subpages: list[dict] | None = None
    extras: dict | None = None


def _build_contents_block(
    max_chars: int,
    use_full_text: bool,
    include_text: list[str] | None,
    exclude_text: list[str] | None,
    summary: bool | str | None,
    livecrawl: str | None,
    subpages: int | None,
    subpage_target: list[str] | None,
    extras: dict | None,
) -> dict:
    """Assemble the Exa `contents` request block.

    Exa accepts text OR highlights (not both useful at once); we pick based on use_full_text.
    Everything else is optional and only included when set.
    """
    text_block: dict = {"maxCharacters": max_chars}
    if include_text:
        text_block["includeHtmlTags"] = False
        text_block["includeText"] = include_text
    if exclude_text:
        text_block["excludeText"] = exclude_text

    contents: dict = {}
    if use_full_text:
        contents["text"] = text_block
    else:
        contents["highlights"] = {"maxCharacters": max_chars}

    if summary is not None and summary is not False:
        # Exa accepts {"query": "..."} or true
        contents["summary"] = {"query": summary} if isinstance(summary, str) else True
    if livecrawl:
        contents["livecrawl"] = livecrawl
    if subpages:
        contents["subpages"] = int(subpages)
    if subpage_target:
        contents["subpageTarget"] = subpage_target
    if extras:
        contents["extras"] = extras
    return contents


def _parse_exa_result(r: dict, max_chars: int) -> ExaResult:
    highlights = r.get("highlights", [])
    text = r.get("text", "") or ""
    excerpt = "\n".join(highlights) if highlights else text[:max_chars]
    return ExaResult(
        title=r.get("title", "") or "",
        url=r.get("url", "") or "",
        excerpt=excerpt,
        published_date=r.get("publishedDate"),
        author=r.get("author"),
        summary=r.get("summary"),
        subpages=r.get("subpages"),
        extras=r.get("extras"),
    )


async def _post_exa(path: str, payload: dict, timeout: float = 20.0) -> dict | None:
    if not EXA_API_KEY:
        logger.warning("Exa API key not configured")
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{EXA_BASE_URL}{path}",
                headers={
                    "x-api-key": EXA_API_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code != 200:
            logger.error("Exa request failed", path=path, status=resp.status_code, body=resp.text[:200])
            return None
        return resp.json()
    except Exception as e:
        logger.error("Exa request error", path=path, error=str(e))
        return None


async def exa_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
    start_crawl_date: str | None = None,
    end_crawl_date: str | None = None,
    search_type: str = "auto",
    use_autoprompt: bool | None = None,
    category: str | None = None,
    use_full_text: bool = False,
    include_text: list[str] | None = None,
    exclude_text: list[str] | None = None,
    summary: bool | str | None = None,
    livecrawl: str | None = None,
    subpages: int | None = None,
    subpage_target: list[str] | None = None,
    extras: dict | None = None,
) -> list[ExaResult]:
    """Search the web via Exa and return clean excerpts.

    See https://docs.exa.ai/reference/search for the full surface.
    """
    payload: dict[str, Any] = {
        "query": query,
        "type": search_type,
        "numResults": num_results,
        "contents": _build_contents_block(
            max_chars=max_chars,
            use_full_text=use_full_text,
            include_text=include_text,
            exclude_text=exclude_text,
            summary=summary,
            livecrawl=livecrawl,
            subpages=subpages,
            subpage_target=subpage_target,
            extras=extras,
        ),
    }

    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date
    if start_crawl_date:
        payload["startCrawlDate"] = start_crawl_date
    if end_crawl_date:
        payload["endCrawlDate"] = end_crawl_date
    if use_autoprompt is not None:
        payload["useAutoprompt"] = use_autoprompt
    if category:
        payload["category"] = category

    data = await _post_exa("/search", payload, timeout=15.0)
    if not data:
        return []
    return [_parse_exa_result(r, max_chars) for r in data.get("results", [])]


async def exa_find_similar(
    url: str,
    num_results: int = 5,
    max_chars: int = 3000,
    exclude_source_domain: bool = True,
    category: str | None = None,
    use_full_text: bool = True,
    include_text: list[str] | None = None,
    exclude_text: list[str] | None = None,
    summary: bool | str | None = None,
    livecrawl: str | None = None,
    subpages: int | None = None,
    subpage_target: list[str] | None = None,
    extras: dict | None = None,
) -> list[ExaResult]:
    """Find pages similar to a given URL."""
    payload: dict[str, Any] = {
        "url": url,
        "numResults": num_results,
        "excludeSourceDomain": exclude_source_domain,
        "contents": _build_contents_block(
            max_chars=max_chars,
            use_full_text=use_full_text,
            include_text=include_text,
            exclude_text=exclude_text,
            summary=summary,
            livecrawl=livecrawl,
            subpages=subpages,
            subpage_target=subpage_target,
            extras=extras,
        ),
    }
    if category:
        payload["category"] = category

    data = await _post_exa("/findSimilar", payload, timeout=15.0)
    if not data:
        return []
    return [_parse_exa_result(r, max_chars) for r in data.get("results", [])]


async def exa_get_contents(
    urls: list[str],
    max_chars: int = 5000,
    include_text: list[str] | None = None,
    exclude_text: list[str] | None = None,
    summary: bool | str | None = None,
    livecrawl: str | None = None,
    subpages: int | None = None,
    subpage_target: list[str] | None = None,
    extras: dict | None = None,
) -> list[ExaResult]:
    """Fetch clean text content from known URLs via Exa /contents endpoint."""
    if not urls:
        return []

    contents = _build_contents_block(
        max_chars=max_chars,
        use_full_text=True,
        include_text=include_text,
        exclude_text=exclude_text,
        summary=summary,
        livecrawl=livecrawl,
        subpages=subpages,
        subpage_target=subpage_target,
        extras=extras,
    )
    payload: dict[str, Any] = {"urls": urls[:10], **contents}

    data = await _post_exa("/contents", payload, timeout=20.0)
    if not data:
        return []
    return [_parse_exa_result(r, max_chars) for r in data.get("results", [])]


@dataclass
class ExaAnswer:
    answer: str
    citations: list[ExaResult]


async def exa_answer(
    query: str,
    model: str | None = None,
    text: bool = False,
    include_text: list[str] | None = None,
) -> ExaAnswer | None:
    """RAG-style answer with citations via Exa /answer endpoint.

    Args:
        query: The natural-language question.
        model: Optional Exa model identifier (e.g. "exa", "exa-pro").
        text: If True, ask Exa to include full text in each citation.
        include_text: Citations must contain these phrases.
    """
    payload: dict[str, Any] = {"query": query}
    if model:
        payload["model"] = model
    if text:
        payload["text"] = True
    if include_text:
        payload["includeText"] = include_text

    data = await _post_exa("/answer", payload, timeout=30.0)
    if not data:
        return None

    citations = [_parse_exa_result(r, 5000) for r in data.get("citations", [])]
    return ExaAnswer(answer=data.get("answer", "") or "", citations=citations)


# ---------------------------------------------------------------------------
# Per-bucket helpers -- pin the right category so callers don't have to remember Exa's enum.
# ---------------------------------------------------------------------------


async def exa_news_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    days: int = 30,
    use_full_text: bool = False,
    **kwargs: Any,
) -> list[ExaResult]:
    """Search news articles, default 30-day window."""
    from datetime import datetime, timedelta, timezone

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return await exa_search(
        query=query,
        num_results=num_results,
        max_chars=max_chars,
        category="news",
        start_published_date=kwargs.pop("start_published_date", start),
        use_full_text=use_full_text,
        **kwargs,
    )


async def exa_research_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    use_full_text: bool = True,
    **kwargs: Any,
) -> list[ExaResult]:
    """Search academic / research papers."""
    return await exa_search(
        query=query,
        num_results=num_results,
        max_chars=max_chars,
        category="research paper",
        use_full_text=use_full_text,
        **kwargs,
    )


async def exa_company_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    use_full_text: bool = False,
    **kwargs: Any,
) -> list[ExaResult]:
    """Search company pages."""
    return await exa_search(
        query=query,
        num_results=num_results,
        max_chars=max_chars,
        category="company",
        use_full_text=use_full_text,
        **kwargs,
    )


async def exa_linkedin_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    use_full_text: bool = False,
    **kwargs: Any,
) -> list[ExaResult]:
    """Search LinkedIn profiles."""
    return await exa_search(
        query=query,
        num_results=num_results,
        max_chars=max_chars,
        category="linkedin profile",
        use_full_text=use_full_text,
        **kwargs,
    )


async def exa_github_search(
    query: str,
    num_results: int = 5,
    max_chars: int = 3000,
    use_full_text: bool = False,
    **kwargs: Any,
) -> list[ExaResult]:
    """Search GitHub repos / code / issues."""
    return await exa_search(
        query=query,
        num_results=num_results,
        max_chars=max_chars,
        category="github",
        use_full_text=use_full_text,
        **kwargs,
    )


def format_exa_results(results: list[ExaResult]) -> str:
    """Format Exa results into a text block for Claude context injection."""
    if not results:
        return "No web search results found."

    chunks = []
    for i, r in enumerate(results, 1):
        header = f"[{i}] {r.title}"
        if r.published_date:
            header += f" ({r.published_date[:10]})"
        body = r.excerpt
        if r.summary:
            body = f"Summary: {r.summary}\n\n{body}"
        chunks.append(f"{header}\nURL: {r.url}\n{body}")

    return "\n\n---\n\n".join(chunks)
