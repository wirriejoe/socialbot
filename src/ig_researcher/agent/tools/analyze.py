"""Video analysis tool using Gemini."""

import asyncio
import base64
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import litellm

from ig_researcher.config import get_settings
from ig_researcher.logging_utils import get_logger
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv(), override=True)

logger = get_logger(__name__)

# Tool definition for Claude API
ANALYZE_VIDEOS_TOOL = {
    "name": "analyze_videos",
    "description": """Analyze video content using Gemini vision AI. Takes video URLs and returns a research-focused summary aligned to the user's query. Use this after fetching video metadata to understand the content without watching the video.""",
    "input_schema": {
        "type": "object",
        "properties": {
            "videos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "shortcode": {"type": "string"},
                        "video_url": {"type": "string"},
                        "caption": {"type": "string"},
                    },
                    "required": ["shortcode", "video_url"],
                },
                "description": "List of videos to analyze",
            },
            "analysis_focus": {
                "type": "string",
                "description": "Research objective or query to focus the analysis on",
                "default": "general insights",
            },
        },
        "required": ["videos"],
    },
}


async def analyze_videos(args: dict[str, Any]) -> dict:
    """
    Analyze videos using Gemini.

    Args:
        args: Tool arguments with videos list and analysis focus

    Returns:
        Tool result with video analyses
    """
    videos = args["videos"]
    focus = args.get("analysis_focus", "general insights")

    settings = get_settings()

    logger.info(
        "tool.analyze: start videos=%s focus=%s concurrency=%s",
        len(videos),
        focus,
        settings.analysis_concurrency,
    )

    if not settings.gemini_api_key:
        logger.info("tool.analyze: missing gemini api key")
        return {
            "content": json.dumps(
                {
                    "error": "Gemini API key not configured",
                    "message": "Set GEMINI_API_KEY in your environment and retry.",
                }
            )
        }

    # Set API key for LiteLLM
    litellm.api_key = settings.gemini_api_key

    concurrency = max(1, settings.analysis_concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_analysis(video: dict[str, Any], client: httpx.AsyncClient) -> dict:
        async with semaphore:
            try:
                shortcode = video.get("shortcode", "unknown")
                logger.info("tool.analyze: analyzing shortcode=%s", shortcode)
                analysis = await _analyze_single_video(
                    video_url=video["video_url"],
                    caption=video.get("caption", ""),
                    focus=focus,
                    http_client=client,
                    shortcode=shortcode,
                )

                return {
                    "shortcode": shortcode,
                    "analysis": analysis,
                    "success": True,
                }

            except Exception as e:
                logger.warning(
                    "tool.analyze: failed shortcode=%s error=%s",
                    video.get("shortcode", "unknown"),
                    e,
                )
                return {
                    "shortcode": video.get("shortcode", "unknown"),
                    "error": str(e),
                    "success": False,
                }

    start_time = time.monotonic()
    async with httpx.AsyncClient(timeout=60.0) as client:
        tasks = [_run_analysis(video, client) for video in videos]
        analyses = await asyncio.gather(*tasks)
    logger.info(
        "tool.analyze: completed analyses in %.2fs",
        time.monotonic() - start_time,
    )

    # Synthesize insights if we have successful analyses
    successful = [a for a in analyses if a["success"]]
    synthesis = None

    if len(successful) >= 2:
        try:
            logger.info(
                "tool.analyze: synthesizing insights for %s videos", len(successful)
            )
            synthesis = await _synthesize_insights(successful, focus)
        except Exception as e:
            synthesis = {"error": f"Synthesis failed: {e!s}"}

    response = {
        "analyzed": len(successful),
        "failed": len(analyses) - len(successful),
        "analyses": analyses,
    }

    if synthesis:
        response["synthesis"] = synthesis

    payload = json.dumps(response)
    logger.info(
        "tool.analyze: done analyzed=%s failed=%s payload_chars=%s",
        response["analyzed"],
        response["failed"],
        len(payload),
    )
    return {"content": payload}


async def _analyze_single_video(
    video_url: str,
    caption: str,
    focus: str,
    http_client: httpx.AsyncClient | None = None,
    shortcode: str | None = None,
) -> dict:
    """Analyze a single video with Gemini."""
    settings = get_settings()
    label = shortcode or "unknown"

    # Download video to temp file
    download_start = time.monotonic()
    if http_client is None:
        async with httpx.AsyncClient(timeout=60.0) as client:
            logger.info("tool.analyze: downloading video shortcode=%s", label)
            response = await client.get(video_url)
            response.raise_for_status()

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
                f.write(response.content)
                video_path = Path(f.name)
    else:
        logger.info("tool.analyze: downloading video shortcode=%s", label)
        response = await http_client.get(video_url)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(response.content)
            video_path = Path(f.name)
    logger.info(
        "tool.analyze: downloaded shortcode=%s bytes=%s time=%.2fs",
        label,
        len(response.content),
        time.monotonic() - download_start,
    )

    try:
        # Read and encode video
        logger.info("tool.analyze: encoding video shortcode=%s", label)
        video_data = video_path.read_bytes()
        video_base64 = base64.b64encode(video_data).decode()

        # Analyze with Gemini
        logger.info("tool.analyze: calling gemini shortcode=%s", label)
        infer_start = time.monotonic()
        response = await litellm.acompletion(
            model=settings.gemini_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": f"data:video/mp4;base64,{video_base64}",
                            },
                        },
                        {
                            "type": "text",
                            "text": f"""You are analyzing an Instagram video to help answer a user research query.

Research objective: {focus}
Caption: {caption}

Extract only information that helps the research objective. The user should NOT need to watch the video. Provide quotes from the video where relevant.

Return JSON with:
- relevance (0-100)
- summary (2-4 sentences, objective-focused)
- key_takeaways (bullet list, max 5)
- venue_or_topic (name(s) or topic extracted, if any)
- group_or_use_case_fit (if relevant, why/why not)
- price_or_budget_signals (if any)
- booking_or_logistics (hours, reservations, location cues, etc.)
- evidence (short phrases tied to what is seen/heard)

If the video is off-topic, set relevance <= 30 and explain why in summary.""",
                        },
                    ],
                }
            ],
            max_tokens=1000,
            response_format={"type": "json_object"},
        )
        logger.info(
            "tool.analyze: gemini completed shortcode=%s time=%.2fs",
            label,
            time.monotonic() - infer_start,
        )

        raw = response.choices[0].message.content
        parsed = None
        try:
            if raw:
                parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"raw": raw}

        return {
            "raw_analysis": raw,
            "parsed": parsed,
            "caption": caption,
        }

    finally:
        # Clean up temp file
        video_path.unlink(missing_ok=True)


async def _synthesize_insights(analyses: list[dict], focus: str) -> dict:
    """Synthesize patterns across all analyzed videos."""
    settings = get_settings()

    formatted = []
    for entry in analyses:
        analysis = entry.get("analysis", {})
        formatted.append(
            json.dumps(
                {
                    "shortcode": entry.get("shortcode"),
                    "url": entry.get("url"),
                    "caption": analysis.get("caption"),
                    "analysis": analysis.get("parsed") or analysis.get("raw_analysis"),
                }
            )
        )
    all_analyses = "\n\n---\n\n".join(formatted)

    response = await litellm.acompletion(
        model=settings.gemini_model,
        messages=[
            {
                "role": "user",
                "content": f"""Synthesize a research report from these {len(analyses)} Instagram video analyses.
Research objective: {focus}

Individual Analyses:
{all_analyses}

Return JSON with:
- executive_summary (3-6 bullets)
- recommendations (max 8). Each item: name, rationale, source_shortcodes, source_urls
- key_insights (list with theme, count, evidence_shortcodes, evidence_urls)
- tradeoffs (list)
- gaps (list)
- evidence_quotes (list of short quotes with source_shortcodes)

Make it decision-ready so the user does not need to watch the videos.""",
            }
        ],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    parsed = None
    try:
        if raw:
            parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    return {
        "synthesis": parsed or {"raw": raw},
        "video_count": len(analyses),
    }
