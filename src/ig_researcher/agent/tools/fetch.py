"""Fetch posts/reels tool for the agent."""

import asyncio
import json
from typing import Any

import httpx
import instaloader
import litellm

from ig_researcher.agent.tools.analyze import _analyze_single_video
from ig_researcher.browser.session import SessionManager
from ig_researcher.config import get_settings
from ig_researcher.logging_utils import get_logger

logger = get_logger(__name__)


# Tool definition for Claude API
FETCH_POSTS_TOOL = {
    "name": "fetch_posts",
    "description": """Fetch detailed metadata for Instagram posts/reels by their shortcodes. Returns video URLs, captions, engagement metrics, and creator info. Use this after searching to get full details for analysis.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "shortcodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of post/reel shortcodes to fetch",
            },
        },
        "required": ["shortcodes"],
    },
}

FETCH_AND_ANALYZE_TOOL = {
    "name": "fetch_and_analyze",
    "description": """Fetch detailed metadata for Instagram posts/reels by shortcode and immediately analyze videos as they are fetched. Returns post metadata plus per-video analyses. Use this to minimize latency by overlapping fetch and analysis.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "shortcodes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of post/reel shortcodes to fetch",
            },
            "analysis_focus": {
                "type": "string",
                "description": "What to focus the analysis on",
                "default": "general insights",
            },
        },
        "required": ["shortcodes"],
    },
}


async def fetch_posts(args: dict[str, Any], session_manager: SessionManager) -> dict:
    """
    Fetch detailed metadata for posts/reels.

    Args:
        args: Tool arguments with shortcodes list
        session_manager: Session manager instance

    Returns:
        Tool result with post metadata
    """
    shortcodes = args["shortcodes"]

    settings = get_settings()
    logger.info(
        "tool.fetch: start shortcodes=%s concurrency=%s authenticated=%s",
        len(shortcodes),
        settings.instaloader_concurrency,
        settings.instaloader_authenticated,
    )
    if settings.instaloader_authenticated and not session_manager.is_authenticated:
        logger.info("tool.fetch: authentication required")
        return {
            "content": json.dumps(
                {
                    "error": "Not authenticated",
                    "message": "Instagram login required. Please sign in in the browser window and retry.",
                }
            )
        }

    concurrency = max(1, settings.instaloader_concurrency)

    def _fetch_single(code: str) -> tuple[dict | None, dict | None]:
        logger.info("tool.fetch: fetching shortcode=%s", code)
        loader = session_manager.get_instaloader_instance(
            authenticated=settings.instaloader_authenticated
        )
        try:
            post = instaloader.Post.from_shortcode(loader.context, code)

            post_data = {
                "shortcode": code,
                "url": f"https://www.instagram.com/p/{code}/",
                "owner_username": post.owner_username,
                "caption": post.caption or "",
                "is_video": post.is_video,
                "likes": post.likes,
                "comments": post.comments,
                "date": post.date.isoformat() if post.date else None,
            }

            if post.is_video:
                post_data["video_url"] = post.video_url
                post_data["video_duration"] = getattr(post, "video_duration", None)

            return post_data, None
        except instaloader.exceptions.InstaloaderException as e:
            logger.warning("tool.fetch: failed shortcode=%s error=%s", code, e)
            return None, {"shortcode": code, "error": str(e)}
        except Exception as e:
            logger.warning("tool.fetch: failed shortcode=%s error=%s", code, e)
            return None, {"shortcode": code, "error": f"Unexpected error: {e!s}"}

    sem = asyncio.Semaphore(concurrency)

    async def _run_one(code: str) -> tuple[dict | None, dict | None]:
        async with sem:
            return await asyncio.to_thread(_fetch_single, code)

    tasks = [_run_one(code) for code in shortcodes]
    results = await asyncio.gather(*tasks)

    posts: list[dict] = []
    errors: list[dict] = []

    for post_data, error in results:
        if post_data:
            posts.append(post_data)
        if error:
            errors.append(error)

    response = {
        "fetched": len(posts),
        "errors": len(errors),
        "posts": posts,
    }

    if errors:
        response["error_details"] = errors

    payload = json.dumps(response)
    logger.info(
        "tool.fetch: completed fetched=%s errors=%s payload_chars=%s",
        len(posts),
        len(errors),
        len(payload),
    )
    return {"content": payload}


async def fetch_and_analyze_posts(
    args: dict[str, Any], session_manager: SessionManager
) -> dict:
    """
    Fetch posts and analyze videos as soon as each post is available.

    Overlaps Instaloader fetch with Gemini analysis to reduce end-to-end latency.
    """
    shortcodes = args["shortcodes"]
    focus = args.get("analysis_focus", "general insights")

    settings = get_settings()
    logger.info(
        "tool.fetch_analyze: start shortcodes=%s fetch_concurrency=%s analysis_concurrency=%s authenticated=%s",
        len(shortcodes),
        settings.instaloader_concurrency,
        settings.analysis_concurrency,
        settings.instaloader_authenticated,
    )

    if settings.instaloader_authenticated and not session_manager.is_authenticated:
        logger.info("tool.fetch_analyze: authentication required")
        return {
            "content": json.dumps(
                {
                    "error": "Not authenticated",
                    "message": "Instagram login required. Please sign in in the browser window and retry.",
                }
            )
        }

    if not settings.gemini_api_key:
        return {
            "content": json.dumps(
                {
                    "error": "Gemini API key not configured",
                    "message": "Set GEMINI_API_KEY in your environment and retry.",
                }
            )
        }

    litellm.api_key = settings.gemini_api_key

    fetch_sem = asyncio.Semaphore(max(1, settings.instaloader_concurrency))
    analyze_sem = asyncio.Semaphore(max(1, settings.analysis_concurrency))

    def _fetch_single(code: str) -> tuple[dict | None, dict | None]:
        loader = session_manager.get_instaloader_instance(
            authenticated=settings.instaloader_authenticated
        )
        try:
            post = instaloader.Post.from_shortcode(loader.context, code)

            post_data = {
                "shortcode": code,
                "url": f"https://www.instagram.com/p/{code}/",
                "owner_username": post.owner_username,
                "caption": post.caption or "",
                "is_video": post.is_video,
                "likes": post.likes,
                "comments": post.comments,
                "date": post.date.isoformat() if post.date else None,
            }

            if post.is_video:
                post_data["video_url"] = post.video_url
                post_data["video_duration"] = getattr(post, "video_duration", None)

            return post_data, None
        except instaloader.exceptions.InstaloaderException as e:
            return None, {"shortcode": code, "error": str(e)}
        except Exception as e:
            return None, {"shortcode": code, "error": f"Unexpected error: {e!s}"}

    async def _process(
        code: str, http_client: httpx.AsyncClient
    ) -> tuple[dict | None, dict | None, dict | None]:
        async with fetch_sem:
            post_data, error = await asyncio.to_thread(_fetch_single, code)

        analysis = None
        if post_data and post_data.get("video_url"):
            async with analyze_sem:
                try:
                    analysis = await _analyze_single_video(
                        video_url=post_data["video_url"],
                        caption=post_data.get("caption", ""),
                        focus=focus,
                        http_client=http_client,
                        shortcode=post_data.get("shortcode"),
                    )
                except Exception as e:
                    analysis = {"error": str(e)}

        return post_data, error, analysis

    posts: list[dict] = []
    errors: list[dict] = []
    analyses: list[dict] = []

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        tasks = [_process(code, http_client) for code in shortcodes]
        results = await asyncio.gather(*tasks)

    for post_data, error, analysis in results:
        if post_data:
            if analysis is not None:
                post_data["analysis"] = analysis
                analyses.append(
                    {
                        "shortcode": post_data["shortcode"],
                        "url": post_data.get("url"),
                        "analysis": analysis,
                    }
                )
            posts.append(post_data)
        if error:
            errors.append(error)

    response = {
        "fetched": len(posts),
        "errors": len(errors),
        "posts": posts,
        "analyses": analyses,
        "analyzed": len(
            [a for a in analyses if not a.get("analysis", {}).get("error")]
        ),
        "analysis_failed": len(
            [a for a in analyses if a.get("analysis", {}).get("error")]
        ),
    }

    if errors:
        response["error_details"] = errors

    payload = json.dumps(response)
    logger.info(
        "tool.fetch_analyze: completed fetched=%s analyses=%s payload_chars=%s",
        len(posts),
        len(analyses),
        len(payload),
    )
    return {"content": payload}
