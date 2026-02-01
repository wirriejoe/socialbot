"""MCP tools for Instagram research."""

from ig_researcher.agent.tools.analyze import ANALYZE_VIDEOS_TOOL, analyze_videos
from ig_researcher.agent.tools.fetch import (
    FETCH_POSTS_TOOL,
    FETCH_AND_ANALYZE_TOOL,
    fetch_posts,
    fetch_and_analyze_posts,
)
from ig_researcher.agent.tools.search import SEARCH_INSTAGRAM_TOOL, search_instagram

# Tool definitions for Claude API
TOOLS = [
    SEARCH_INSTAGRAM_TOOL,
    FETCH_POSTS_TOOL,
    FETCH_AND_ANALYZE_TOOL,
    ANALYZE_VIDEOS_TOOL,
]

__all__ = [
    # Tool functions
    "search_instagram",
    "fetch_posts",
    "fetch_and_analyze_posts",
    "analyze_videos",
    # Tool definitions
    "SEARCH_INSTAGRAM_TOOL",
    "FETCH_POSTS_TOOL",
    "FETCH_AND_ANALYZE_TOOL",
    "ANALYZE_VIDEOS_TOOL",
    "TOOLS",
]
