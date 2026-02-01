"""MCP server exposing IG Researcher tools."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from ig_researcher.agent.tools.analyze import analyze_videos
from ig_researcher.agent.tools.fetch import fetch_and_analyze_posts, fetch_posts
from ig_researcher.agent.tools.search import search_instagram
from ig_researcher.browser.session import PersistentBrowserSession, SessionManager
from ig_researcher.config import get_settings
from ig_researcher.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class AppContext:
    session_manager: SessionManager
    persistent_session: PersistentBrowserSession
    browser_lock: asyncio.Lock


@asynccontextmanager
async def _lifespan(server: FastMCP):
    settings = get_settings()
    session_manager = SessionManager()
    persistent_session = PersistentBrowserSession(
        session_manager,
        browser_engine=settings.browser_engine,
        cdp_url=settings.chrome_cdp_url,
    )
    browser_lock = asyncio.Lock()

    try:
        yield AppContext(
            session_manager=session_manager,
            persistent_session=persistent_session,
            browser_lock=browser_lock,
        )
    finally:
        await persistent_session.close()


mcp = FastMCP("ig-researcher", lifespan=_lifespan, json_response=True)


def _parse_tool_payload(result: dict) -> dict:
    raw = result.get("content", "")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {"error": "Empty tool response"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "Invalid tool response", "raw": raw}


async def _ensure_browser(app_ctx: AppContext, ctx: Context | None) -> None:
    async with app_ctx.browser_lock:
        if ctx:
            await ctx.info("Starting browser (headful)...")
        ready = await app_ctx.persistent_session.ensure_ready(check_login=False)
        if not ready:
            raise RuntimeError("Failed to start browser session.")
        if ctx:
            await ctx.info("Browser ready.")


async def _ensure_gemini(ctx: Context | None) -> dict | None:
    settings = get_settings()
    if settings.gemini_api_key:
        return None
    if ctx:
        await ctx.info("Gemini API key missing. Set GEMINI_API_KEY and retry.")
    return {
        "error": "Gemini API key not configured",
        "message": "Set GEMINI_API_KEY in your environment and retry.",
    }


@mcp.tool(name="search_instagram")
async def mcp_search_instagram(
    query: str,
    limit: int = 20,
    content_type: str = "all",
    ctx: Context | None = None,
) -> dict:
    """Search Instagram for posts/reels matching a query."""
    app_ctx: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    await _ensure_browser(app_ctx, ctx)

    if ctx:
        await ctx.info(f"Searching Instagram for '{query}'")

    result = await search_instagram(
        {"query": query, "limit": limit, "content_type": content_type},
        app_ctx.session_manager,
        app_ctx.persistent_session,
    )
    payload = _parse_tool_payload(result)
    if ctx:
        await ctx.info(f"Search complete. Found {payload.get('count', 0)} results.")
    return payload


@mcp.tool(name="fetch_posts")
async def mcp_fetch_posts(
    shortcodes: list[str],
    ctx: Context | None = None,
) -> dict:
    """Fetch detailed metadata for Instagram posts/reels by shortcode."""
    app_ctx: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    if ctx:
        await ctx.info(f"Fetching {len(shortcodes)} posts")
    result = await fetch_posts({"shortcodes": shortcodes}, app_ctx.session_manager)
    payload = _parse_tool_payload(result)
    if ctx:
        await ctx.info(f"Fetch complete. Retrieved {payload.get('fetched', 0)} posts.")
    return payload


@mcp.tool(name="fetch_and_analyze")
async def mcp_fetch_and_analyze(
    shortcodes: list[str],
    analysis_focus: str = "general insights",
    ctx: Context | None = None,
) -> dict:
    """Fetch posts and analyze videos as they are retrieved."""
    app_ctx: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    if ctx:
        await ctx.info(
            f"Fetching and analyzing {len(shortcodes)} posts (focus: {analysis_focus})"
        )
    gemini_error = await _ensure_gemini(ctx)
    if gemini_error:
        return gemini_error
    result = await fetch_and_analyze_posts(
        {"shortcodes": shortcodes, "analysis_focus": analysis_focus},
        app_ctx.session_manager,
    )
    payload = _parse_tool_payload(result)
    if ctx:
        await ctx.info(
            "Fetch+analyze complete. "
            f"Fetched {payload.get('fetched', 0)} posts, "
            f"analyzed {payload.get('analyzed', 0)} videos."
        )
    return payload


@mcp.tool(name="analyze_videos")
async def mcp_analyze_videos(
    videos: list[dict[str, Any]],
    analysis_focus: str = "general insights",
    ctx: Context | None = None,
) -> dict:
    """Analyze videos with Gemini (vision)."""
    if ctx:
        await ctx.info(f"Analyzing {len(videos)} videos (focus: {analysis_focus})")
    gemini_error = await _ensure_gemini(ctx)
    if gemini_error:
        return gemini_error
    result = await analyze_videos(
        {
            "videos": videos,
            "analysis_focus": analysis_focus,
        }
    )
    payload = _parse_tool_payload(result)
    if ctx:
        await ctx.info(
            f"Analysis complete. Analyzed {payload.get('analyzed', 0)} videos."
        )
    return payload


@mcp.tool(name="research_socials")
async def mcp_research_socials(
    query: str,
    limit: int = 20,
    content_type: str = "all",
    analysis_focus: str = "general insights",
    ctx: Context | None = None,
) -> dict:
    """Run a full Instagram research workflow (search → fetch+analyze)."""
    app_ctx: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    await _ensure_browser(app_ctx, ctx)

    if ctx:
        await ctx.info(f"Researching Instagram for '{query}' (limit={limit})")

    search_result = await search_instagram(
        {"query": query, "limit": limit, "content_type": content_type},
        app_ctx.session_manager,
        app_ctx.persistent_session,
    )
    search_payload = _parse_tool_payload(search_result)

    if search_payload.get("error") or search_payload.get("challenge_required"):
        return {
            "stage": "search",
            "query": query,
            "limit": limit,
            "content_type": content_type,
            "search": search_payload,
        }

    results = search_payload.get("results", []) or []
    shortcodes = [item.get("shortcode") for item in results if item.get("shortcode")]
    if not shortcodes:
        return {
            "stage": "search",
            "query": query,
            "limit": limit,
            "content_type": content_type,
            "search": search_payload,
            "message": "No results found.",
        }

    if ctx:
        await ctx.info(f"Fetching + analyzing {len(shortcodes)} posts")
    gemini_error = await _ensure_gemini(ctx)
    if gemini_error:
        return {
            "stage": "analysis",
            "query": query,
            "limit": limit,
            "content_type": content_type,
            "analysis_focus": analysis_focus,
            "search": search_payload,
            "analysis": gemini_error,
        }

    analysis_result = await fetch_and_analyze_posts(
        {"shortcodes": shortcodes, "analysis_focus": analysis_focus},
        app_ctx.session_manager,
    )
    analysis_payload = _parse_tool_payload(analysis_result)

    return {
        "stage": "complete",
        "query": query,
        "limit": limit,
        "content_type": content_type,
        "analysis_focus": analysis_focus,
        "search": search_payload,
        "analysis": analysis_payload,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
