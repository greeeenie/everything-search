import asyncio
import html
import re
import textwrap
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Annotated, Any

import httpx
from pydantic import Field

Query = Annotated[str, Field(min_length=1, max_length=300)]


async def get_json(client, path, params):
    if path.startswith("http") and urlsplit(path).hostname != client.base_url.host:
        raise ValueError("API URL outside the selected source.")
    try:
        response = await client.get(path, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as error:
        raise ValueError(f"{client.base_url.host} returned HTTP {error.response.status_code}.") from None
    except (httpx.RequestError, ValueError):
        raise ValueError(f"{client.base_url.host} did not return readable results.") from None


def shorten(text, width=420):
    return textwrap.shorten(text or "", width, placeholder="…")


async def search_hacker_news(
    query: Query,
    max_results: Annotated[int, Field(ge=1, le=10)] = 6,
) -> dict[str, Any]:
    """Find relevant Hacker News stories and comments with links to individual comments."""
    query = query.strip()
    if not query:
        raise ValueError("Enter a non-empty search query.")
    async with httpx.AsyncClient(base_url="https://hn.algolia.com/api/v1/", timeout=10,
                                 proxy=os.getenv("RESEARCH_EU_PROXY") or None, trust_env=False) as client:
        stories, comments = await asyncio.gather(*(
            get_json(client, "search", {"query": query, "tags": tag, "hitsPerPage": max_results})
            for tag in ("story", "comment")
        ))
    hn = "https://news.ycombinator.com/item?id="
    return {
        "query": query,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified",
        "source": "https://hn.algolia.com/api/v1/",
        "scope": "Separate ranked samples of stories and comments, not a full census.",
        "stories": [{
            "title": item["title"],
            "url": hn + item["objectID"],
            "published_at": item["created_at"],
            "comments_total": item.get("num_comments"),
        } for item in stories["hits"]],
        "comments": [{
            "text": shorten(html.unescape(re.sub(r"<[^>]+>", " ", item["comment_text"]))),
            "url": hn + item["objectID"],
            "thread_url": hn + str(item["story_id"]),
            "thread_title": item.get("story_title"),
            "published_at": item["created_at"],
        } for item in comments["hits"] if item.get("comment_text") and item.get("story_id")],
    }


async def read_github_issue(client, issue):
    result = {
        "title": issue["title"],
        "url": issue["html_url"],
        "state": issue["state"],
        "published_at": issue["created_at"],
        "updated_at": issue["updated_at"],
        "description": shorten(issue.get("body")),
        "comments_total": issue["comments"],
        "comments": [],
    }
    if issue["comments"] > 0:
        try:
            comments = await get_json(client, issue["comments_url"], {"per_page": 4})
        except ValueError as error:
            result["comments_error"] = str(error)
        else:
            result["comments"] = [{
                "text": shorten(comment.get("body")),
                "url": comment["html_url"],
                "published_at": comment["created_at"],
            } for comment in comments]
    return result


async def search_github_issues(
    query: Query,
    repository: Annotated[str | None, Field(
        max_length=150, pattern=r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$",
        description="Optional owner/repository, for example microsoft/playwright.",
    )] = None,
    max_results: Annotated[int, Field(ge=1, le=5)] = 4,
) -> dict[str, Any]:
    """Search public GitHub issues and sample their comments. For GitHub Discussions use search_web."""
    query = query.strip()
    if not query:
        raise ValueError("Enter a non-empty search query.")
    search_query = f"{query} is:issue"
    if repository:
        search_query += f" repo:{repository}"
    async with httpx.AsyncClient(
        base_url="https://api.github.com/",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Research-MCP"},
        timeout=10,
        proxy=os.getenv("RESEARCH_EU_PROXY") or None,
        trust_env=False,
    ) as client:
        response = await get_json(client, "search/issues", {"q": search_query, "per_page": max_results})
        issues = await asyncio.gather(*(read_github_issue(client, issue) for issue in response["items"]))
    return {
        "query": query,
        "repository": repository,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified",
        "source": "https://api.github.com/",
        "scope": "Up to four comments per issue. Issue reports represent a sample, not defect rates.",
        "issues": issues,
    }
