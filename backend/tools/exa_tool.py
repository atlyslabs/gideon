"""
Exa Search Tools -- web search and similar page discovery via Exa API.

Replaces Claude's built-in web_search server tool with Exa's neural search.
Always available (no per-user integration needed).
"""

from typing import Any

from tools.internal_tools import InternalTool
from services.exa_service import (
    exa_search,
    exa_find_similar,
    exa_get_contents,
    exa_answer,
    exa_news_search,
    exa_research_search,
    exa_company_search,
    exa_linkedin_search,
    exa_github_search,
)


def _serialize_result(r) -> dict:
    out = {
        "title": r.title,
        "url": r.url,
        "excerpt": r.excerpt,
        "published_date": r.published_date,
    }
    if r.author:
        out["author"] = r.author
    if r.summary:
        out["summary"] = r.summary
    if r.subpages:
        out["subpages"] = r.subpages
    if r.extras:
        out["extras"] = r.extras
    return out


# Schema fragments reused across search-shaped tools.
_COMMON_SEARCH_KNOBS: dict[str, dict] = {
    "num_results": {
        "type": "integer",
        "description": "Number of results (default 5, max 10)",
        "default": 5,
    },
    "include_domains": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Only search these domains (e.g. ['linkedin.com', 'github.com']).",
    },
    "exclude_domains": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Exclude these domains from results.",
    },
    "start_published_date": {
        "type": "string",
        "description": "Only results published after this date (ISO format, e.g. '2026-01-01').",
    },
    "end_published_date": {
        "type": "string",
        "description": "Only results published before this date (ISO format).",
    },
    "start_crawl_date": {
        "type": "string",
        "description": "Only results crawled by Exa after this date (ISO format).",
    },
    "end_crawl_date": {
        "type": "string",
        "description": "Only results crawled by Exa before this date (ISO format).",
    },
    "use_autoprompt": {
        "type": "boolean",
        "description": "Let Exa rewrite the query for better neural matching.",
    },
    "search_type": {
        "type": "string",
        "enum": ["auto", "neural", "keyword"],
        "description": "Retrieval mode. 'auto' is fine 99% of the time.",
    },
    "include_text": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Results MUST contain these substrings (max 1 phrase, <= 5 words).",
    },
    "exclude_text": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Results must NOT contain these substrings.",
    },
    "summary": {
        "type": ["boolean", "string"],
        "description": "If true/string, ask Exa for a summary per result. String value = focusing question.",
    },
    "livecrawl": {
        "type": "string",
        "enum": ["never", "fallback", "always", "auto"],
        "description": "Force fresh crawl: 'always' = re-fetch each result; 'fallback' if cache empty; 'auto' is Exa default.",
    },
    "subpages": {
        "type": "integer",
        "description": "Also fetch up to N subpages per result.",
    },
    "subpage_target": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Subpage keywords to prefer when crawling subpages (e.g. ['pricing', 'about']).",
    },
}


def _extract_search_kwargs(params: dict) -> dict:
    """Pull the optional knobs from a params dict for forwarding to exa_search."""
    kwargs: dict[str, Any] = {}
    for key in (
        "include_domains",
        "exclude_domains",
        "start_published_date",
        "end_published_date",
        "start_crawl_date",
        "end_crawl_date",
        "include_text",
        "exclude_text",
        "summary",
        "livecrawl",
        "subpages",
        "subpage_target",
        "use_autoprompt",
    ):
        if params.get(key) is not None:
            kwargs[key] = params[key]
    if params.get("search_type"):
        kwargs["search_type"] = params["search_type"]
    return kwargs


class WebSearch(InternalTool):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def description(self) -> str:
        return (
            "Search the public web for current information, news, documentation, "
            "tutorials, people profiles, company info, and general knowledge. "
            "Returns relevant excerpts from web pages. Use targeted, specific queries. "
            "For category-specific lookups prefer the dedicated tools "
            "(news_search, research_search, company_search, linkedin_search, github_search)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query. Natural language works best.",
                },
                "category": {
                    "type": "string",
                    "description": "Dedicated index. Most callers should use the per-bucket tools instead.",
                    "enum": [
                        "people",
                        "company",
                        "news",
                        "research paper",
                        "linkedin profile",
                        "github",
                        "tweet",
                        "pdf",
                        "financial report",
                    ],
                },
                **_COMMON_SEARCH_KNOBS,
            },
            "required": ["query"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        query = (params.get("query") or "").strip()
        if not query:
            return {"error": "query is required."}

        num = params.get("num_results")
        num = int(num) if num is not None else 5

        results = await exa_search(
            query=query,
            num_results=min(num, 10),
            category=params.get("category"),
            **_extract_search_kwargs(params),
        )

        if not results:
            return {"results": [], "summary": "No results found for this query."}

        return {
            "results": [_serialize_result(r) for r in results],
            "count": len(results),
        }


class FindSimilarPages(InternalTool):
    @property
    def name(self) -> str:
        return "find_similar_pages"

    @property
    def description(self) -> str:
        return (
            "Find web pages similar to a given URL. Useful for finding related articles, "
            "competitor pages, or alternative sources on the same topic. "
            "By default excludes the source domain so you get diverse results."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to find similar pages for.",
                },
                "num_results": {
                    "type": "integer",
                    "description": "Number of results (default 5, max 10).",
                    "default": 5,
                },
                "exclude_source_domain": {
                    "type": "boolean",
                    "description": "Exclude the source URL's domain. Default true. Set false to keep same-site results.",
                    "default": True,
                },
                "category": {
                    "type": "string",
                    "description": "Pin to a dedicated index.",
                    "enum": [
                        "people",
                        "company",
                        "news",
                        "research paper",
                        "linkedin profile",
                        "github",
                    ],
                },
                "summary": _COMMON_SEARCH_KNOBS["summary"],
                "livecrawl": _COMMON_SEARCH_KNOBS["livecrawl"],
                "subpages": _COMMON_SEARCH_KNOBS["subpages"],
                "subpage_target": _COMMON_SEARCH_KNOBS["subpage_target"],
                "include_text": _COMMON_SEARCH_KNOBS["include_text"],
                "exclude_text": _COMMON_SEARCH_KNOBS["exclude_text"],
            },
            "required": ["url"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        url = (params.get("url") or "").strip()
        if not url:
            return {"error": "url is required."}

        num = params.get("num_results")
        num = int(num) if num is not None else 5
        exclude_source = params.get("exclude_source_domain")
        if exclude_source is None:
            exclude_source = True

        results = await exa_find_similar(
            url=url,
            num_results=min(num, 10),
            exclude_source_domain=bool(exclude_source),
            category=params.get("category"),
            summary=params.get("summary"),
            livecrawl=params.get("livecrawl"),
            subpages=params.get("subpages"),
            subpage_target=params.get("subpage_target"),
            include_text=params.get("include_text"),
            exclude_text=params.get("exclude_text"),
        )

        if not results:
            return {"results": [], "summary": "No similar pages found."}

        return {
            "results": [_serialize_result(r) for r in results],
            "count": len(results),
        }


class GetPageContent(InternalTool):
    @property
    def name(self) -> str:
        return "get_page_content"

    @property
    def description(self) -> str:
        return (
            "Fetch clean text content from one or more web page URLs. "
            "Use when you have a URL and need the full page text (not just a snippet from search). "
            "Supports summaries, live re-crawl, and subpage discovery."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs to fetch content from (max 10).",
                },
                "summary": _COMMON_SEARCH_KNOBS["summary"],
                "livecrawl": _COMMON_SEARCH_KNOBS["livecrawl"],
                "subpages": _COMMON_SEARCH_KNOBS["subpages"],
                "subpage_target": _COMMON_SEARCH_KNOBS["subpage_target"],
                "include_text": _COMMON_SEARCH_KNOBS["include_text"],
                "exclude_text": _COMMON_SEARCH_KNOBS["exclude_text"],
            },
            "required": ["urls"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        urls = params.get("urls") or []
        if not isinstance(urls, list):
            urls = [str(urls)]
        if not urls:
            return {"error": "urls is required."}

        results = await exa_get_contents(
            urls=urls[:10],
            summary=params.get("summary"),
            livecrawl=params.get("livecrawl"),
            subpages=params.get("subpages"),
            subpage_target=params.get("subpage_target"),
            include_text=params.get("include_text"),
            exclude_text=params.get("exclude_text"),
        )

        if not results:
            return {"results": [], "summary": "Could not fetch content from the provided URLs."}

        return {
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "content": r.excerpt,
                    **({"summary": r.summary} if r.summary else {}),
                    **({"subpages": r.subpages} if r.subpages else {}),
                }
                for r in results
            ],
            "count": len(results),
        }


class ExaAnswer(InternalTool):
    @property
    def name(self) -> str:
        return "web_answer"

    @property
    def description(self) -> str:
        return (
            "Ask Exa for a RAG-style answer with citations. Best for direct factual questions "
            "(e.g. 'What was Anthropic's most recent funding round?'). Returns an answer string plus "
            "a list of source URLs Exa used. Use web_search instead when you want to read the sources yourself."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language question to answer.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional Exa model identifier (e.g. 'exa', 'exa-pro').",
                },
                "text": {
                    "type": "boolean",
                    "description": "If true, include full text for each citation (more tokens).",
                    "default": False,
                },
                "include_text": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Citations must contain these substrings.",
                },
            },
            "required": ["query"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        query = (params.get("query") or "").strip()
        if not query:
            return {"error": "query is required."}

        result = await exa_answer(
            query=query,
            model=params.get("model"),
            text=bool(params.get("text", False)),
            include_text=params.get("include_text"),
        )
        if result is None:
            return {"error": "Exa /answer is unavailable or returned no answer."}

        return {
            "answer": result.answer,
            "citations": [_serialize_result(r) for r in result.citations],
            "citation_count": len(result.citations),
        }


# ---------------------------------------------------------------------------
# Per-bucket search helpers -- pin the right category.
# ---------------------------------------------------------------------------


def _bucket_schema(extra: dict | None = None) -> dict:
    """Schema for the per-bucket search tools (subset of WebSearch knobs)."""
    base = {
        "query": {
            "type": "string",
            "description": "Search query in natural language.",
        },
        "num_results": _COMMON_SEARCH_KNOBS["num_results"],
        "include_domains": _COMMON_SEARCH_KNOBS["include_domains"],
        "exclude_domains": _COMMON_SEARCH_KNOBS["exclude_domains"],
        "start_published_date": _COMMON_SEARCH_KNOBS["start_published_date"],
        "end_published_date": _COMMON_SEARCH_KNOBS["end_published_date"],
        "use_full_text": {
            "type": "boolean",
            "description": "Return full page text instead of highlights (slower, more tokens).",
        },
        "summary": _COMMON_SEARCH_KNOBS["summary"],
        "livecrawl": _COMMON_SEARCH_KNOBS["livecrawl"],
        "include_text": _COMMON_SEARCH_KNOBS["include_text"],
    }
    if extra:
        base.update(extra)
    return {"type": "object", "properties": base, "required": ["query"]}


async def _run_bucket(fn, params: dict, *, default_full_text: bool = False, extra: dict | None = None) -> dict:
    query = (params.get("query") or "").strip()
    if not query:
        return {"error": "query is required."}

    num = params.get("num_results")
    num = int(num) if num is not None else 5
    full_text = params.get("use_full_text")
    if full_text is None:
        full_text = default_full_text

    forward: dict[str, Any] = {
        "query": query,
        "num_results": min(num, 10),
        "use_full_text": bool(full_text),
    }
    for key in (
        "include_domains",
        "exclude_domains",
        "start_published_date",
        "end_published_date",
        "summary",
        "livecrawl",
        "include_text",
    ):
        if params.get(key) is not None:
            forward[key] = params[key]
    if extra:
        forward.update(extra)

    results = await fn(**forward)
    if not results:
        return {"results": [], "summary": "No results found for this query."}
    return {
        "results": [_serialize_result(r) for r in results],
        "count": len(results),
    }


class ExaNewsSearch(InternalTool):
    @property
    def name(self) -> str:
        return "news_search"

    @property
    def description(self) -> str:
        return (
            "Search recent news articles via Exa (auto 30-day window). "
            "Use for current events, company announcements, market news. "
            "Override `days` to widen or narrow the window."
        )

    @property
    def input_schema(self) -> dict:
        return _bucket_schema(extra={
            "days": {
                "type": "integer",
                "description": "Lookback window in days (default 30).",
                "default": 30,
            },
        })

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        days = params.get("days")
        extra = {"days": int(days)} if days is not None else None
        return await _run_bucket(exa_news_search, params, default_full_text=False, extra=extra)


class ExaResearchSearch(InternalTool):
    @property
    def name(self) -> str:
        return "research_search"

    @property
    def description(self) -> str:
        return (
            "Search academic / research papers via Exa (pinned to the 'research paper' index). "
            "Returns full text by default so you can read methodology and findings."
        )

    @property
    def input_schema(self) -> dict:
        return _bucket_schema()

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        return await _run_bucket(exa_research_search, params, default_full_text=True)


class ExaCompanySearch(InternalTool):
    @property
    def name(self) -> str:
        return "company_search"

    @property
    def description(self) -> str:
        return (
            "Search company / business pages via Exa (pinned to the 'company' index). "
            "Best for company research: products, pricing, leadership, market position."
        )

    @property
    def input_schema(self) -> dict:
        return _bucket_schema()

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        return await _run_bucket(exa_company_search, params, default_full_text=False)


class ExaLinkedInSearch(InternalTool):
    @property
    def name(self) -> str:
        return "linkedin_search"

    @property
    def description(self) -> str:
        return (
            "Search LinkedIn profiles via Exa (pinned to the 'linkedin profile' index). "
            "Use for person lookup: role, employer, background. Returns public profile excerpts only."
        )

    @property
    def input_schema(self) -> dict:
        return _bucket_schema()

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        return await _run_bucket(exa_linkedin_search, params, default_full_text=False)


class ExaGitHubSearch(InternalTool):
    @property
    def name(self) -> str:
        return "github_search"

    @property
    def description(self) -> str:
        return (
            "Search GitHub repositories, issues, and code via Exa (pinned to the 'github' index). "
            "Use for open-source library discovery, code examples, issue debugging."
        )

    @property
    def input_schema(self) -> dict:
        return _bucket_schema()

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        return await _run_bucket(exa_github_search, params, default_full_text=False)
