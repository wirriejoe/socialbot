"""MCP server exposing IG Researcher tools."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP

from ig_researcher.agent.tools.fetch import fetch_and_analyze_posts
from ig_researcher.agent.tools.search import search_instagram
from ig_researcher.browser.session import PersistentBrowserSession, SessionManager
from ig_researcher.config import get_settings
from ig_researcher.keychain import load_gemini_key, store_gemini_key
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


def _summarize_searches(search_payloads: list[dict]) -> list[dict]:
    summary: list[dict] = []
    for entry in search_payloads:
        payload = entry.get("payload", {}) or {}
        summary.append(
            {
                "query": entry.get("query"),
                "count": payload.get("count"),
                "requested_limit": payload.get("requested_limit"),
                "exhausted": payload.get("exhausted"),
                "error": payload.get("error"),
                "auth_required": payload.get("auth_required"),
                "challenge_required": payload.get("challenge_required"),
            }
        )
    return summary


def _summarize_analysis(analysis_payload: dict) -> dict:
    analyses = analysis_payload.get("analyses", []) or []
    analyzed_shortcodes = [
        item.get("shortcode")
        for item in analyses
        if isinstance(item, dict) and item.get("shortcode")
    ]
    return {
        "fetched": analysis_payload.get("fetched"),
        "analyzed": analysis_payload.get("analyzed"),
        "analysis_failed": analysis_payload.get("analysis_failed"),
        "analyzed_shortcodes": analyzed_shortcodes,
        "synthesis": analysis_payload.get("synthesis"),
        "error": analysis_payload.get("error"),
        "message": analysis_payload.get("message"),
    }


def _compact_result(result: dict) -> dict:
    compact: dict = {}
    for key in (
        "stage",
        "queries",
        "limit",
        "limit_per_query",
        "analysis_limit",
        "content_type",
        "analysis_focus",
        "total_deduped",
        "analysis_truncated",
        "deduped",
        "errors",
        "message",
        "result_path",
        "result_bytes",
        "result_saved",
    ):
        if key in result:
            compact[key] = result[key]
    if "searches" in result:
        compact["search_summary"] = _summarize_searches(result.get("searches", []))
    if "analysis" in result and isinstance(result.get("analysis"), dict):
        compact["analysis_summary"] = _summarize_analysis(result["analysis"])
    return compact


def _persist_result(result: dict, persist_path: str | None) -> tuple[str, int]:
    settings = get_settings()
    if persist_path:
        path = Path(persist_path).expanduser()
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = settings.cache_dir / f"research_{timestamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=True, indent=2)
    path.write_text(payload, encoding="utf-8")
    return str(path), len(payload)


async def _ensure_browser(app_ctx: AppContext, ctx: Context | None) -> None:
    async with app_ctx.browser_lock:
        if ctx:
            await ctx.info("Starting browser (headful)...")
        ready = await app_ctx.persistent_session.ensure_ready(check_login=False)
        if not ready:
            raise RuntimeError("Failed to start browser session.")
        if ctx:
            await ctx.info("Browser ready.")


def _requires_user_action(payload: dict) -> bool:
    return bool(
        payload.get("auth_required")
        or payload.get("challenge_required")
        or payload.get("error") in {"Not authenticated", "Challenge required"}
    )


async def _maybe_close_browser(
    app_ctx: AppContext,
    ctx: Context | None,
    close_browser: bool,
    keep_open_reason: str | None = None,
) -> None:
    if not close_browser:
        return
    if keep_open_reason and ctx:
        await ctx.info(f"Keeping browser open: {keep_open_reason}")
        return
    if ctx:
        await ctx.info("Closing browser.")
    await app_ctx.persistent_session.close()


async def _ensure_gemini(ctx: Context | None) -> dict | None:
    settings = get_settings()
    if settings.gemini_api_key:
        return None
    try:
        keychain_key = load_gemini_key()
    except Exception:
        keychain_key = None

    if keychain_key:
        os.environ["GEMINI_API_KEY"] = keychain_key
        settings.gemini_api_key = keychain_key
        if ctx:
            await ctx.info("Loaded Gemini API key from Keychain.")
        return None
    if ctx:
        await ctx.info("Gemini API key missing. Set GEMINI_API_KEY and retry.")
    return {
        "error": "Gemini API key not configured",
        "message": "Set GEMINI_API_KEY in your environment and retry.",
    }


@mcp.tool(name="configure_gemini_key")
async def mcp_configure_gemini_key(
    api_key: str,
    ctx: Context | None = None,
) -> dict:
    """Store the Gemini API key in macOS Keychain."""
    try:
        store_gemini_key(api_key)
    except Exception as exc:
        if ctx:
            await ctx.info(f"Failed to store Gemini key: {exc}")
        return {
            "error": "Failed to store Gemini API key",
            "message": str(exc),
        }
    os.environ["GEMINI_API_KEY"] = api_key
    settings = get_settings()
    settings.gemini_api_key = api_key
    if ctx:
        await ctx.info("Gemini API key stored in Keychain.")
    return {"ok": True}


@mcp.tool(name="search_instagram")
async def mcp_search_instagram(
    query: str,
    limit: int = 20,
    content_type: str = "all",
    close_browser: bool = True,
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
    await _maybe_close_browser(
        app_ctx,
        ctx,
        close_browser,
        keep_open_reason="login or challenge required"
        if _requires_user_action(payload)
        else None,
    )
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


@mcp.tool(name="research_socials")
async def mcp_research_socials(
    query: str | None = None,
    queries: list[str] | None = None,
    limit: int = 20,
    limit_per_query: int | None = None,
    analysis_limit: int | None = None,
    content_type: str = "all",
    analysis_focus: str = "general insights",
    return_compact: bool = False,
    persist_results: bool = False,
    persist_path: str | None = None,
    max_payload_chars: int = 20000,
    close_browser: bool = True,
    ctx: Context | None = None,
) -> dict:
    """Run a full Instagram research workflow (multi-search → reduce → fetch+analyze).

    Use `analysis_limit` to cap how many deduped results are analyzed while preserving
    the full deduped list in the output.
    """
    app_ctx: AppContext = ctx.request_context.lifespan_context  # type: ignore[assignment]
    await _ensure_browser(app_ctx, ctx)

    query_list: list[str] = []
    if queries:
        query_list.extend([q for q in queries if q])
    if query and query not in query_list:
        query_list.insert(0, query)

    if not query_list:
        result = {
            "stage": "search",
            "error": "Missing query",
            "message": "Provide `query` or `queries` to start research.",
        }
        if persist_results:
            result_path, result_bytes = _persist_result(result, persist_path)
            result["result_path"] = result_path
            result["result_bytes"] = result_bytes
            result["result_saved"] = True
        return result

    if limit_per_query is None:
        limit_per_query = limit

    if analysis_limit is None:
        analysis_limit = limit

    if analysis_focus == "general insights" and query_list:
        analysis_focus = query_list[0]

    if ctx:
        await ctx.info(
            f"Researching Instagram for {len(query_list)} query(s) (limit={limit})"
        )

    search_payloads: list[dict] = []
    aggregated_results: list[dict] = []
    search_errors: list[dict] = []
    user_action_required = False

    for q in query_list:
        if ctx:
            await ctx.info(f"Searching '{q}' (limit={limit_per_query})")
        search_result = await search_instagram(
            {"query": q, "limit": limit_per_query, "content_type": content_type},
            app_ctx.session_manager,
            app_ctx.persistent_session,
        )
        payload = _parse_tool_payload(search_result)
        search_payloads.append({"query": q, "payload": payload})

        if payload.get("error") or payload.get("challenge_required"):
            search_errors.append({"query": q, "error": payload})
            if _requires_user_action(payload):
                user_action_required = True
            continue

        results = payload.get("results", []) or []
        for item in results:
            aggregated_results.append(
                {
                    "shortcode": item.get("shortcode"),
                    "type": item.get("type"),
                    "url": item.get("url"),
                    "query": q,
                }
            )

    if not aggregated_results:
        result = {
            "stage": "search",
            "queries": query_list,
            "limit": limit,
            "limit_per_query": limit_per_query,
            "analysis_limit": analysis_limit,
            "content_type": content_type,
            "searches": search_payloads,
            "errors": search_errors,
            "message": "No results found.",
        }
        await _maybe_close_browser(
            app_ctx,
            ctx,
            close_browser,
            keep_open_reason="login or challenge required"
            if user_action_required
            else None,
        )
        if persist_results:
            result_path, result_bytes = _persist_result(result, persist_path)
            result["result_path"] = result_path
            result["result_bytes"] = result_bytes
            result["result_saved"] = True
        payload = json.dumps(result, ensure_ascii=True)
        if len(payload) > max_payload_chars:
            if not result.get("result_saved"):
                result_path, result_bytes = _persist_result(result, persist_path)
                result["result_path"] = result_path
                result["result_bytes"] = result_bytes
                result["result_saved"] = True
            return _compact_result(result)
        return _compact_result(result) if return_compact else result

    seen: dict[str, dict] = {}
    deduped: list[dict] = []
    for item in aggregated_results:
        code = item.get("shortcode")
        if not code:
            continue
        if code not in seen:
            entry = {
                "shortcode": code,
                "type": item.get("type"),
                "url": item.get("url"),
                "queries": [item.get("query")],
            }
            seen[code] = entry
            deduped.append(entry)
        else:
            if item.get("query") not in seen[code]["queries"]:
                seen[code]["queries"].append(item.get("query"))

    deduped_all = deduped
    if analysis_limit:
        deduped = deduped_all[:analysis_limit]
    else:
        deduped = deduped_all

    shortcodes = [item.get("shortcode") for item in deduped if item.get("shortcode")]
    if not shortcodes:
        result = {
            "stage": "search",
            "queries": query_list,
            "limit": limit,
            "limit_per_query": limit_per_query,
            "analysis_limit": analysis_limit,
            "content_type": content_type,
            "searches": search_payloads,
            "errors": search_errors,
            "message": "No results found.",
        }
        await _maybe_close_browser(
            app_ctx,
            ctx,
            close_browser,
            keep_open_reason="login or challenge required"
            if user_action_required
            else None,
        )
        if persist_results:
            result_path, result_bytes = _persist_result(result, persist_path)
            result["result_path"] = result_path
            result["result_bytes"] = result_bytes
            result["result_saved"] = True
        payload = json.dumps(result, ensure_ascii=True)
        if len(payload) > max_payload_chars:
            if not result.get("result_saved"):
                result_path, result_bytes = _persist_result(result, persist_path)
                result["result_path"] = result_path
                result["result_bytes"] = result_bytes
                result["result_saved"] = True
            return _compact_result(result)
        return _compact_result(result) if return_compact else result

    if ctx:
        await ctx.info(f"Fetching + analyzing {len(shortcodes)} posts")
    gemini_error = await _ensure_gemini(ctx)
    if gemini_error:
        result = {
            "stage": "analysis",
            "queries": query_list,
            "limit": limit,
            "limit_per_query": limit_per_query,
            "analysis_limit": analysis_limit,
            "content_type": content_type,
            "analysis_focus": analysis_focus,
            "searches": search_payloads,
            "errors": search_errors,
            "deduped": deduped,
            "deduped_all": deduped_all,
            "total_deduped": len(deduped_all),
            "analysis_truncated": len(deduped_all) > len(deduped),
            "analysis": gemini_error,
        }
        await _maybe_close_browser(
            app_ctx,
            ctx,
            close_browser,
            keep_open_reason="login or challenge required"
            if user_action_required
            else None,
        )
        if persist_results:
            result_path, result_bytes = _persist_result(result, persist_path)
            result["result_path"] = result_path
            result["result_bytes"] = result_bytes
            result["result_saved"] = True
        payload = json.dumps(result, ensure_ascii=True)
        if len(payload) > max_payload_chars:
            if not result.get("result_saved"):
                result_path, result_bytes = _persist_result(result, persist_path)
                result["result_path"] = result_path
                result["result_bytes"] = result_bytes
                result["result_saved"] = True
            return _compact_result(result)
        return _compact_result(result) if return_compact else result

    analysis_result = await fetch_and_analyze_posts(
        {"shortcodes": shortcodes, "analysis_focus": analysis_focus},
        app_ctx.session_manager,
    )
    analysis_payload = _parse_tool_payload(analysis_result)

    result = {
        "stage": "complete",
        "queries": query_list,
        "limit": limit,
        "limit_per_query": limit_per_query,
        "analysis_limit": analysis_limit,
        "content_type": content_type,
        "analysis_focus": analysis_focus,
        "searches": search_payloads,
        "errors": search_errors,
        "deduped": deduped,
        "deduped_all": deduped_all,
        "total_deduped": len(deduped_all),
        "analysis_truncated": len(deduped_all) > len(deduped),
        "analysis": analysis_payload,
    }
    await _maybe_close_browser(
        app_ctx,
        ctx,
        close_browser,
        keep_open_reason="login or challenge required"
        if user_action_required
        else None,
    )
    if persist_results:
        result_path, result_bytes = _persist_result(result, persist_path)
        result["result_path"] = result_path
        result["result_bytes"] = result_bytes
        result["result_saved"] = True
    payload = json.dumps(result, ensure_ascii=True)
    if len(payload) > max_payload_chars:
        if not result.get("result_saved"):
            result_path, result_bytes = _persist_result(result, persist_path)
            result["result_path"] = result_path
            result["result_bytes"] = result_bytes
            result["result_saved"] = True
        return _compact_result(result)
    return _compact_result(result) if return_compact else result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
