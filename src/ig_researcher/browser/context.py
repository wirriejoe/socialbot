"""Browser context management with Chrome and Camoufox."""

import platform
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import async_playwright

from ig_researcher.logging_utils import get_logger

logger = get_logger(__name__)

class BrowserMode(Enum):
    """Browser display modes."""

    HEADLESS = auto()  # Camoufox headless
    HEADED = auto()  # Chrome headed (interactive login)
    CAMOUFOX = auto()  # Camoufox headed
    VIRTUAL = auto()  # Camoufox headless with virtual display (Linux)


def _get_os_name() -> str:
    """Get OS name for Camoufox config."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "linux"


@dataclass
class BrowserConfig:
    """Configuration for browser instance."""

    mode: BrowserMode = BrowserMode.HEADLESS
    humanize: bool = True
    enable_cache: bool = True
    user_data_dir: Path | None = None
    cdp_url: str | None = None


class BrowserContextManager:
    """
    Manages browser context lifecycle.

    Uses:
    - User's Chrome for HEADED mode (familiar browser for interactive login)
    - Camoufox for HEADLESS/CAMOUFOX/VIRTUAL modes (stealth automation)

    Handles:
    - Browser instantiation with fingerprint config
    - Persistent context for session continuity
    - Proper cleanup on exit
    """

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._browser = None
        self._context = None
        self._page = None
        self._cm = None  # Store the context manager
        self._playwright = None  # For Playwright mode
        self._using_cdp = False

    async def __aenter__(self) -> "BrowserContextManager":
        """Enter async context, starting browser."""
        logger.info(
            "browser.context: starting mode=%s cdp_url=%s",
            self.config.mode.name.lower(),
            self.config.cdp_url or "none",
        )
        if self.config.cdp_url:
            await self._attach_chrome_cdp()
        elif self.config.mode == BrowserMode.HEADED:
            # Use Chrome for headed mode (user's familiar browser)
            await self._start_chrome_browser()
        else:
            # Use Camoufox for automation (headless/headful/virtual)
            await self._start_camoufox_browser()

        return self

    async def _start_chrome_browser(self) -> None:
        """Launch Chrome for interactive login."""
        logger.info("browser.context: launching chrome")
        self._playwright = await async_playwright().start()

        # Use dedicated profile directory for ig-researcher (works even if Chrome is running)
        if self.config.user_data_dir:
            self.config.user_data_dir.mkdir(parents=True, exist_ok=True)
            profile_dir = self.config.user_data_dir
        else:
            # Fallback to temp profile
            profile_dir = None

        if profile_dir:
            logger.info("browser.context: using profile dir %s", profile_dir)
            # Launch with persistent context (saves cookies automatically)
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",  # Uses user's installed Chrome
                headless=False,
                locale="en-US",
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._browser = None
        else:
            logger.info("browser.context: using temp chrome profile")
            self._browser = await self._playwright.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._context = await self._browser.new_context(locale="en-US")

    async def _attach_chrome_cdp(self) -> None:
        """Attach to an existing Chrome instance via CDP."""
        logger.info("browser.context: attaching to chrome cdp=%s", self.config.cdp_url)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(
            self.config.cdp_url
        )
        self._using_cdp = True

        # Prefer existing context to preserve the active Chrome profile/session.
        if self._browser.contexts:
            logger.info("browser.context: using existing chrome context")
            self._context = self._browser.contexts[0]
        else:
            logger.info("browser.context: no existing context, creating new")
            self._context = await self._browser.new_context(locale="en-US")

    async def _start_camoufox_browser(self) -> None:
        """Start Camoufox browser for stealth automation."""
        logger.info("browser.context: launching camoufox")
        os_name = _get_os_name()

        if self.config.mode == BrowserMode.CAMOUFOX:
            headless: bool | str = False
        elif self.config.mode == BrowserMode.VIRTUAL:
            headless = "virtual"
        else:
            headless = True

        kwargs = {
            "headless": headless,
            "humanize": self.config.humanize,
            "os": os_name,
            "locale": ["en-US", "en"],
            "i_know_what_im_doing": True,
            "enable_cache": self.config.enable_cache,
        }

        if self.config.user_data_dir:
            kwargs["persistent_context"] = True
            kwargs["user_data_dir"] = str(self.config.user_data_dir)

        self._cm = AsyncCamoufox(**kwargs)
        self._browser = await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context, cleaning up browser."""
        logger.info("browser.context: closing browser")
        if self._cm:
            # Camoufox cleanup
            await self._cm.__aexit__(exc_type, exc_val, exc_tb)
            self._cm = None
        elif self._playwright:
            # Playwright/Chrome cleanup
            if self._context and not self._using_cdp:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            await self._playwright.stop()
            self._playwright = None

        self._browser = None
        self._context = None
        self._page = None
        self._using_cdp = False

    async def new_page(self):
        """Create a new page in the context."""
        if self._context:
            # Playwright mode
            self._page = await self._context.new_page()
        elif self._browser:
            # Camoufox mode
            self._page = await self._browser.new_page()
        else:
            raise RuntimeError("Browser not started. Use 'async with' context manager.")
        return self._page

    async def get_cookies(self, urls: list[str] | None = None) -> list[dict]:
        """Get all cookies or cookies for specific URLs."""
        if not self._page:
            await self.new_page()

        if self._context:
            # Playwright mode
            return await self._context.cookies(urls)
        else:
            # Camoufox mode
            return await self._page.context.cookies(urls)

    async def add_cookies(self, cookies: list[dict]) -> None:
        """Add cookies to the context."""
        if not self._page:
            await self.new_page()

        if self._context:
            await self._context.add_cookies(cookies)
        else:
            await self._page.context.add_cookies(cookies)

    async def save_storage_state(self, path: Path) -> dict:
        """Save complete storage state (cookies + localStorage)."""
        if not self._page:
            await self.new_page()

        if self._context:
            return await self._context.storage_state(path=str(path))
        else:
            return await self._page.context.storage_state(path=str(path))
