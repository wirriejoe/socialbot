"""High-level browser client for Instagram operations."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from ig_researcher.browser.actions import InstagramActions, SearchResult
from ig_researcher.browser.context import (
    BrowserConfig,
    BrowserContextManager,
    BrowserMode,
)
from ig_researcher.browser.humanize import HumanBehavior, HumanizationConfig
from ig_researcher.browser.rate_limiter import RateLimiter
from ig_researcher.browser.session import SessionManager
from ig_researcher.logging_utils import get_logger

if TYPE_CHECKING:
    from ig_researcher.browser.session import PersistentBrowserSession

logger = get_logger(__name__)

@dataclass
class BrowserClientConfig:
    """Configuration for BrowserClient."""

    headless: bool = False  # Default to headful for better Instagram compatibility
    humanize: bool = True
    browser_engine: str = "chrome"  # "chrome" or "camoufox"
    cdp_url: str | None = None


class BrowserClient:
    """
    High-level browser automation client for Instagram operations.

    Provides a clean API for:
    - Interactive login
    - Content search
    - Metadata fetching

    Usage:
        async with BrowserClient(session_manager) as client:
            # Interactive login (headed mode)
            await client.interactive_login()

            # Search (headful mode by default)
            async for result in client.search("sustainable fashion"):
                print(result.shortcode)

        # Or with persistent session (browser stays open):
        async with BrowserClient(session_manager, persistent_session=ps) as client:
            async for result in client.search("query"):
                print(result.shortcode)
    """

    def __init__(
        self,
        session_manager: SessionManager,
        config: BrowserClientConfig | None = None,
        persistent_session: Optional["PersistentBrowserSession"] = None,
        on_challenge: Optional[Callable[..., Awaitable[bool]]] = None,
    ):
        self.session_manager = session_manager
        self.config = config or BrowserClientConfig()
        self._persistent_session = persistent_session
        self._on_challenge = on_challenge

        self._humanizer = HumanBehavior()
        self._rate_limiter = RateLimiter()
        self._context: BrowserContextManager | None = None
        self._actions: InstagramActions | None = None
        self._owns_context = False  # Track if we created the context

    async def __aenter__(self) -> "BrowserClient":
        """Enter async context, preparing browser."""
        if self._persistent_session and self._persistent_session.browser_context:
            # Use the persistent session's browser context
            logger.info("browser.client: using persistent session context")
            self._context = self._persistent_session.browser_context
            self._owns_context = False

            # Use fast mode for humanization - no delays needed in headful mode
            self._humanizer = HumanBehavior(HumanizationConfig(fast_mode=True))

            # Set up challenge handler from persistent session
            on_challenge = (
                self._on_challenge
                or self._persistent_session.wait_for_challenge_resolution
            )
        else:
            # Create our own browser context
            logger.info("browser.client: creating new browser context")
            engine = self.config.browser_engine.lower()
            if self.config.cdp_url:
                mode = BrowserMode.HEADED
            elif self.config.headless:
                mode = BrowserMode.HEADLESS
            elif engine == "camoufox":
                mode = BrowserMode.CAMOUFOX
            else:
                mode = BrowserMode.HEADED

            if self.config.cdp_url:
                user_data_dir = None
            elif mode in (
                BrowserMode.HEADLESS,
                BrowserMode.CAMOUFOX,
                BrowserMode.VIRTUAL,
            ):
                user_data_dir = self.session_manager.get_browser_data_dir("camoufox")
            else:
                user_data_dir = self.session_manager.get_browser_data_dir("chrome")

            browser_config = BrowserConfig(
                mode=mode,
                humanize=self.config.humanize,
                user_data_dir=user_data_dir,
                cdp_url=self.config.cdp_url,
            )
            logger.info(
                "browser.client: context mode=%s engine=%s cdp_url=%s",
                mode.name.lower(),
                engine,
                self.config.cdp_url or "none",
            )
            self._context = BrowserContextManager(browser_config)
            await self._context.__aenter__()
            self._owns_context = True
            on_challenge = self._on_challenge

            if self.config.headless:
                self._humanizer = HumanBehavior()
            else:
                self._humanizer = HumanBehavior(HumanizationConfig(fast_mode=True))

            bootstrap_cookies = self.session_manager.get_bootstrap_cookies()
            if bootstrap_cookies and not self.config.cdp_url:
                logger.info(
                    "browser.client: seeding context with %s cookies",
                    len(bootstrap_cookies),
                )
                await self._context.add_cookies(bootstrap_cookies)

        self._actions = InstagramActions(
            self._context,
            self._humanizer,
            self._rate_limiter,
            on_challenge=on_challenge,
            keep_page_open=bool(self._persistent_session),
        )

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context, cleaning up browser."""
        if self._context and self._owns_context:
            # Only close if we created the context (not using persistent session)
            await self._context.__aexit__(exc_type, exc_val, exc_tb)
        self._context = None
        self._actions = None

    async def interactive_login(self, timeout: int = 300) -> bool:
        """
        Open headed browser for user to manually log in.

        Args:
            timeout: Maximum time to wait for login (seconds)

        Returns:
            True if login successful
        """
        return await self.session_manager.interactive_login(timeout)

    async def search(
        self,
        query: str,
        content_type: str = "reels",
        limit: int = 20,
    ) -> AsyncIterator[SearchResult]:
        """
        Search Instagram and yield results with human-like behavior.

        Args:
            query: Search term
            content_type: "all", "reels", or "posts"
            limit: Maximum results to return

        Yields:
            SearchResult objects
        """
        if not self._actions:
            raise RuntimeError(
                "BrowserClient not initialized. Use 'async with' context."
            )

        async for result in self._actions.search(query, content_type, limit):
            yield result

    async def get_post_metadata(self, shortcode: str) -> Optional[dict]:
        """
        Get metadata for a single post/reel.

        Args:
            shortcode: Instagram post/reel shortcode

        Returns:
            Post metadata dict or None if not found
        """
        if not self._actions:
            raise RuntimeError(
                "BrowserClient not initialized. Use 'async with' context."
            )

        return await self._actions.get_post_metadata(shortcode)

    async def export_cookies(self) -> list[dict]:
        """
        Export cookies for sharing with other tools (e.g., Instaloader).

        Returns:
            List of cookie dicts
        """
        if not self._context:
            raise RuntimeError(
                "BrowserClient not initialized. Use 'async with' context."
            )

        cookies = await self._context.get_cookies()
        self.session_manager.update_cookies(cookies)
        return cookies
