"""Instagram-specific browser actions."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from ig_researcher.browser.context import BrowserContextManager
from ig_researcher.browser.humanize import HumanBehavior
from ig_researcher.browser.rate_limiter import RateLimiter
from ig_researcher.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class SearchResult:
    """A single search result from Instagram."""

    shortcode: str
    content_type: str  # "reel" or "post"
    url: str
    thumbnail_url: str | None = None


class AuthenticationRequiredError(RuntimeError):
    """Raised when Instagram requires a login to proceed."""


class ChallengeRequiredError(RuntimeError):
    """Raised when Instagram presents a challenge or checkpoint."""


class InstagramActions:
    """
    Instagram-specific browser actions.

    Encapsulates:
    - Search navigation and result extraction
    - Profile viewing
    - Content discovery
    """

    SELECTORS = {
        "search_results": 'a[href*="/reel/"], a[href*="/p/"]',
        "reel_links": 'a[href*="/reel/"]',
        "post_links": 'a[href*="/p/"]',
        "login_form": 'form[id="loginForm"]',
        "logged_in_indicator": 'a[href*="/direct/inbox/"], svg[aria-label="Direct"]',
    }

    CHALLENGE_INDICATORS = [
        "checkpoint",
        "challenge",
        "suspicious",
        "verify",
        "confirm it's you",
        "security check",
        "unusual activity",
    ]

    CHALLENGE_SELECTORS = [
        'form[action*="/challenge/"]',
        'input[name="security_code"]',
        'input[name="verificationCode"]',
        'input[name="email_or_phone"]',
    ]

    def __init__(
        self,
        context_manager: BrowserContextManager,
        humanizer: HumanBehavior | None = None,
        rate_limiter: RateLimiter | None = None,
        on_challenge: Callable[..., Awaitable[bool]] | None = None,
        keep_page_open: bool = False,
    ):
        self.context = context_manager
        self.humanizer = humanizer or HumanBehavior()
        self.rate_limiter = rate_limiter or RateLimiter()
        self._on_challenge = on_challenge
        self._keep_page_open = keep_page_open
        self._active_page = None

    async def check_for_challenge(self, page) -> bool:
        """
        Check if current page is a challenge/captcha page.

        Args:
            page: Playwright page object

        Returns:
            True if on a challenge page
        """
        url = page.url.lower()

        # URL-based detection
        if any(indicator in url for indicator in ["challenge", "checkpoint"]):
            return True

        # DOM-based detection (faster and fewer false positives than full HTML scan)
        for selector in self.CHALLENGE_SELECTORS:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        return False

    async def search(
        self,
        query: str,
        content_type: str = "all",
        limit: int = 20,
    ) -> AsyncIterator[SearchResult]:
        """
        Search Instagram and yield results.

        Args:
            query: Search term
            content_type: "all", "reels", or "posts"
            limit: Maximum results to return
        """
        import urllib.parse

        # Check rate limits
        await self.rate_limiter.wait_if_needed("search")
        logger.info(
            "actions.search: start query=%s content_type=%s limit=%s",
            query,
            content_type,
            limit,
        )

        if self._keep_page_open and self._active_page:
            page = self._active_page
            if hasattr(page, "is_closed") and page.is_closed():
                page = await self.context.new_page()
                self._active_page = page
        else:
            page = await self.context.new_page()
            if self._keep_page_open:
                self._active_page = page

        try:
            # Navigate to search with URL-encoded query
            encoded_query = urllib.parse.quote(query)
            search_url = (
                f"https://www.instagram.com/explore/search/keyword/?q={encoded_query}"
            )
            logger.info("actions.search: navigating to search url")
            await page.goto(search_url, timeout=30000, wait_until="domcontentloaded")

            if await self._is_login_required(page):
                self.rate_limiter.record_failure("search", is_rate_limit=False)
                logger.info("actions.search: login required")
                raise AuthenticationRequiredError("Instagram login required")

            # Check for challenge/captcha page (do not block the tool call)
            if await self.check_for_challenge(page):
                self.rate_limiter.record_failure("search", is_rate_limit=False)
                logger.info("actions.search: challenge detected")
                raise ChallengeRequiredError(
                    "Instagram challenge detected. Please resolve it in the browser and retry."
                )

            # Brief wait for page to load
            await self.humanizer.random_delay(1, 2)

            # Wait for any content links to appear
            selector = self.SELECTORS["search_results"]
            try:
                logger.info("actions.search: waiting for results selector")
                await page.wait_for_selector(
                    selector,
                    timeout=10000,
                    state="attached",
                )
            except Exception:
                logger.info("actions.search: selector timeout, no results")
                self.rate_limiter.record_action("search")
                return

            async def _collect_items() -> list[dict]:
                items = await page.evaluate(
                    """(selector) => {
                    const links = Array.from(document.querySelectorAll(selector));
                    return links.map(a => {
                        const href = a.href;
                        const match = href.match(/\\/(reel|p)\\/([^\\/?#]+)/);
                        const shortcode = match ? match[2] : null;
                        const type = match ? match[1] : (href.includes('/reel/') ? 'reel' : 'post');
                        const img = a.querySelector('img');
                        return {
                            shortcode,
                            content_type: type === 'reel' ? 'reel' : 'post',
                            url: href,
                            thumbnail_url: img ? img.src : null
                        };
                    }).filter(item => item.shortcode);
                }""",
                    selector,
                )
                return list(items or [])

            def _merge_items(items: list[dict], seen: dict[str, dict]) -> None:
                for item in items:
                    key = item.get("shortcode")
                    if not key:
                        continue
                    existing = seen.get(key)
                    if not existing:
                        seen[key] = item
                    else:
                        if not existing.get("thumbnail_url") and item.get("thumbnail_url"):
                            existing["thumbnail_url"] = item["thumbnail_url"]

            # Scroll progressively until we collect enough results or stop growing.
            seen: dict[str, dict] = {}
            initial_items = await _collect_items()
            _merge_items(initial_items, seen)
            current_count = len(seen)
            logger.info("actions.search: initial results=%s", current_count)

            if limit > current_count:
                max_scrolls = min(40, max(5, math.ceil(limit / 10) + 6))
                logger.info("actions.search: scrolling up to %s times", max_scrolls)
                no_growth = 0

                for scroll_idx in range(1, max_scrolls + 1):
                    if hasattr(page, "is_closed") and page.is_closed():
                        logger.info("actions.search: page closed, reopening")
                        page = await self.context.new_page()
                        if self._keep_page_open:
                            self._active_page = page
                        await page.goto(
                            search_url, timeout=30000, wait_until="domcontentloaded"
                        )
                        await self.humanizer.random_delay(0.8, 1.4)

                    await self.humanizer.human_scroll(page)
                    await self.humanizer.random_delay(0.7, 1.2)

                    try:
                        await page.wait_for_function(
                            "(selector, prev) => document.querySelectorAll(selector).length > prev",
                            timeout=8000,
                            arg=[selector, current_count],
                        )
                    except Exception:
                        # Swallow timeouts; we will re-check counts below.
                        pass

                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass

                    new_items = await _collect_items()
                    _merge_items(new_items, seen)
                    new_count = len(seen)
                    delta = new_count - current_count
                    logger.info(
                        "actions.search: scroll=%s unique=%s new=%s no_growth=%s",
                        scroll_idx,
                        new_count,
                        max(delta, 0),
                        no_growth,
                    )

                    if new_count <= current_count:
                        no_growth += 1
                        if no_growth >= 3:
                            logger.info("actions.search: no new results, stopping scroll")
                            break
                    else:
                        no_growth = 0
                        current_count = new_count

                    if current_count >= limit:
                        logger.info("actions.search: reached requested limit=%s", limit)
                        break

            # Extract results from the accumulated set to avoid DOM virtualization loss.
            results = list(seen.values())
            if content_type != "all":
                if content_type == "reels":
                    results = [item for item in results if item.get("content_type") == "reel"]
                else:
                    results = [item for item in results if item.get("content_type") == "post"]

            results = results[:limit]

            self.rate_limiter.record_action("search")
            logger.info(
                "actions.search: extracted %s results (unique=%s requested=%s)",
                len(results),
                len(seen),
                limit,
            )

            for result in results:
                yield SearchResult(**result)

        except Exception as e:
            is_rate_limit = "rate" in str(e).lower() or "429" in str(e)
            self.rate_limiter.record_failure("search", is_rate_limit)
            logger.warning("actions.search: failed error=%s", e)
            raise

        finally:
            if self._keep_page_open:
                return

            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except Exception:
                # Avoid hanging on page close; leave cleanup to context shutdown.
                pass

    async def _is_login_required(self, page) -> bool:
        """Check if Instagram is requesting a login."""
        url = page.url.lower()
        if "login" in url or "accounts/login" in url:
            return True

        login_form = await page.query_selector(self.SELECTORS["login_form"])
        if login_form:
            return True

        logged_in_indicator = await page.query_selector(
            self.SELECTORS["logged_in_indicator"]
        )
        if logged_in_indicator:
            return False

        try:
            cookies = await page.context.cookies(["https://www.instagram.com/"])
        except Exception:
            cookies = []
        return not any(
            cookie.get("name") == "sessionid" and cookie.get("value")
            for cookie in cookies
        )

    def _get_content_selector(self, content_type: str) -> str:
        """Get CSS selector for content type."""
        if content_type == "reels":
            return self.SELECTORS["reel_links"]
        elif content_type == "posts":
            return self.SELECTORS["post_links"]
        return self.SELECTORS["search_results"]

    async def wait_for_login(self, page, timeout: int = 300) -> bool:
        """
        Wait for user to complete manual login.

        Returns True if login successful.
        """
        try:
            await page.wait_for_selector(
                self.SELECTORS["logged_in_indicator"],
                timeout=timeout * 1000,
            )

            await self.humanizer.random_delay(2, 4)

            is_logged_in = await page.query_selector(
                self.SELECTORS["logged_in_indicator"]
            )
            return is_logged_in is not None

        except TimeoutError:
            return False

    async def check_for_action_block(self, page) -> bool:
        """Check if we've been action-blocked."""
        content = await page.content()
        block_indicators = [
            "try again later",
            "action blocked",
            "we restrict certain activity",
            "unusual activity",
        ]
        content_lower = content.lower()
        return any(indicator in content_lower for indicator in block_indicators)

    async def get_post_metadata(self, shortcode: str) -> dict | None:
        """
        Get metadata for a single post/reel.

        Args:
            shortcode: Instagram post/reel shortcode

        Returns:
            Post metadata dict or None if not found
        """
        await self.rate_limiter.wait_if_needed("profile_view")

        page = await self.context.new_page()

        try:
            url = f"https://www.instagram.com/p/{shortcode}/"
            await page.goto(url)
            await self.humanizer.random_delay(1, 2)

            # Extract basic metadata from page
            metadata = await page.evaluate(
                """() => {
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const script of scripts) {
                    try {
                        const data = JSON.parse(script.textContent);
                        if (data['@type'] === 'VideoObject' || data['@type'] === 'ImageObject') {
                            return data;
                        }
                    } catch {}
                }
                return null;
            }"""
            )

            self.rate_limiter.record_action("profile_view")
            return metadata

        except Exception as e:
            is_rate_limit = "rate" in str(e).lower() or "429" in str(e)
            self.rate_limiter.record_failure("profile_view", is_rate_limit)
            return None

        finally:
            await page.close()
