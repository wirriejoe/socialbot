"""Instagram search tool for the agent."""

import json
from typing import TYPE_CHECKING, Any, Optional

from ig_researcher.browser.actions import (
    AuthenticationRequiredError,
    ChallengeRequiredError,
)
from ig_researcher.browser.client import BrowserClient
from ig_researcher.browser.session import SessionManager
from ig_researcher.logging_utils import get_logger

if TYPE_CHECKING:
    from ig_researcher.browser.session import PersistentBrowserSession

logger = get_logger(__name__)


# Tool definition for Claude API
SEARCH_INSTAGRAM_TOOL = {
    "name": "search_instagram",
    "description": """Search Instagram for content matching a query. Returns a list of post/reel shortcodes that can be fetched for analysis. If login is required, it will return an authentication error.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (e.g., 'sustainable fashion', 'cooking tutorials')",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return",
                "default": 20,
            },
            "content_type": {
                "type": "string",
                "enum": ["all", "reels", "posts"],
                "description": "Type of content to search for",
                "default": "all",
            },
        },
        "required": ["query"],
    },
}


async def search_instagram(
    args: dict[str, Any],
    session_manager: SessionManager,
    persistent_session: Optional["PersistentBrowserSession"] = None,
) -> dict:
    """
    Search Instagram for content.

    Args:
        args: Tool arguments with query, limit, and content_type
        session_manager: Session manager instance
        persistent_session: Optional persistent browser session (headful Chrome)

    Returns:
        Tool result with search results
    """
    query = args["query"]
    limit = args.get("limit", 20)
    content_type = args.get("content_type", "all")

    results = []

    try:
        logger.info(
            "tool.search: start query=%s content_type=%s limit=%s",
            query,
            content_type,
            limit,
        )
        async with BrowserClient(
            session_manager, persistent_session=persistent_session
        ) as client:
            async for result in client.search(query, content_type, limit):
                results.append({
                    "shortcode": result.shortcode,
                    "type": result.content_type,
                    "url": result.url,
                })

            if not results and content_type != "all":
                logger.info("tool.search: no results, retrying with content_type=all")
                # Fallback to all content types if filters were too strict.
                content_type = "all"
                async for result in client.search(query, content_type, limit):
                    results.append({
                        "shortcode": result.shortcode,
                        "type": result.content_type,
                        "url": result.url,
                    })
    except AuthenticationRequiredError:
        logger.info("tool.search: authentication required")
        return {
            "content": json.dumps({
                "error": "Not authenticated",
                "message": "Instagram login required. Please sign in in the browser window and retry.",
                "auth_required": True,
            })
        }
    except ChallengeRequiredError:
        logger.info("tool.search: challenge required")
        return {
            "content": json.dumps({
                "error": "Challenge required",
                "message": "Instagram checkpoint/challenge detected. Please resolve it in the browser window and retry.",
                "challenge_required": True,
            })
        }

    response = {
        "query": query,
        "content_type": content_type,
        "requested_limit": limit,
        "count": len(results),
        "exhausted": len(results) < limit,
        "results": results,
    }

    payload = json.dumps(response)
    logger.info(
        "tool.search: completed count=%s requested=%s exhausted=%s payload_chars=%s",
        len(results),
        limit,
        len(results) < limit,
        len(payload),
    )
    return {"content": payload}
