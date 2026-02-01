"""Session management bridging browser and Instaloader."""

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import instaloader

from ig_researcher.browser.context import (
    BrowserConfig,
    BrowserContextManager,
    BrowserMode,
)
from ig_researcher.config import get_settings
from ig_researcher.logging_utils import get_logger
from ig_researcher.storage.session_store import SessionStore

logger = get_logger(__name__)

@dataclass
class SessionInfo:
    """Information about the current session."""

    username: str | None = None
    is_authenticated: bool = False
    last_used: datetime | None = None


def _get_chrome_instagram_cookies() -> list[dict]:
    """Extract Instagram cookies from user's Chrome browser."""
    try:
        import browser_cookie3

        # Get cookies from Chrome for Instagram domain
        chrome_cookies = browser_cookie3.chrome(domain_name=".instagram.com")

        cookies = []
        for cookie in chrome_cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": bool(cookie.secure),
                    "httpOnly": bool(
                        getattr(cookie, "_rest", {}).get("HttpOnly", False)
                    ),
                    "sameSite": "Lax",
                }
            )
        return cookies
    except Exception:
        # browser_cookie3 can fail if Chrome is locked or cookies are inaccessible
        return []


class SessionManager:
    """
    Manages Instagram session across browser and Instaloader.

    Features:
    - Encrypted cookie storage
    - Session validation
    - Cookie format conversion for Instaloader
    - Interactive login via browser
    - Import existing session from Chrome
    """

    REQUIRED_COOKIES = ["sessionid", "csrftoken", "ds_user_id", "mid", "ig_did"]

    def __init__(self, profile_name: str = "default"):
        self.settings = get_settings()
        self.profile_name = profile_name
        self.profile_dir = self.settings.profiles_dir / profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self._session_store = SessionStore(self.settings.data_dir, profile_name)
        self._browser_data_dir = self.profile_dir / "browser"

    def get_browser_data_dir(self, engine: str = "chrome") -> Path:
        """Directory for browser profiles by engine."""
        engine_name = engine.lower()
        if engine_name == "chrome":
            path = self._browser_data_dir
        else:
            path = self._browser_data_dir / engine_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def browser_data_dir(self) -> Path:
        """Directory for Chrome persistent context."""
        return self.get_browser_data_dir("chrome")

    @property
    def is_authenticated(self) -> bool:
        """Check if we have a valid session."""
        return self._session_store.has_session

    def get_session_info(self) -> SessionInfo:
        """Get current session metadata."""
        cookies = self._session_store.load_cookies()
        return SessionInfo(
            username=cookies.get("ds_user"),
            is_authenticated=bool(cookies.get("sessionid")),
            last_used=self._session_store.get_last_updated(),
        )

    def update_cookies(self, browser_cookies: list[dict]) -> None:
        """Update stored cookies from browser export."""
        self._session_store.update_from_browser(browser_cookies)

    def _session_cookies_for_browser(self) -> list[dict]:
        """Convert stored session cookies into Playwright cookie dicts."""
        stored = self._session_store.load_cookies()
        cookies = []
        for name, value in stored.items():
            if name.startswith("_"):
                continue
            if not name or value is None:
                continue
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
            )
        return cookies

    def get_bootstrap_cookies(self) -> list[dict]:
        """
        Get cookies for seeding a fresh browser profile.

        Merges stored session cookies with the user's Chrome cookies.
        Stored cookies take precedence.
        """
        merged: dict[tuple[str, str, str], dict] = {}

        chrome_cookies = _get_chrome_instagram_cookies()
        logger.info("session: loaded %s chrome cookies", len(chrome_cookies))
        for cookie in chrome_cookies:
            if not cookie.get("name") or cookie.get("value") is None:
                continue
            key = (
                cookie.get("name", ""),
                cookie.get("domain", ""),
                cookie.get("path", "/"),
            )
            merged[key] = cookie

        stored_cookies = self._session_cookies_for_browser()
        logger.info("session: loaded %s stored cookies", len(stored_cookies))
        for cookie in stored_cookies:
            key = (
                cookie.get("name", ""),
                cookie.get("domain", ""),
                cookie.get("path", "/"),
            )
            merged[key] = cookie

        logger.info("session: merged %s bootstrap cookies", len(merged))
        return list(merged.values())

    async def interactive_login(self, timeout: int = 300) -> bool:
        """
        Open browser for user to log in interactively.

        If user is already logged into Instagram in Chrome, imports that session.

        Args:
            timeout: Maximum time to wait for login (seconds)

        Returns:
            True if login successful
        """
        import asyncio

        config = BrowserConfig(
            mode=BrowserMode.HEADED,
            humanize=True,
            user_data_dir=self.get_browser_data_dir("chrome"),
        )

        bootstrap_cookies = self.get_bootstrap_cookies()

        async with BrowserContextManager(config) as browser:
            page = await browser.new_page()

            if bootstrap_cookies:
                await browser.add_cookies(bootstrap_cookies)

            await page.goto("https://www.instagram.com/")
            await asyncio.sleep(2)

            # Check if already logged in (from imported cookies or previous session)
            current_url = page.url
            cookies = await browser.get_cookies()
            session_cookie = next(
                (c for c in cookies if c["name"] == "sessionid" and c.get("value")),
                None,
            )

            if session_cookie and "login" not in current_url.lower():
                # Already logged in - save and return
                self.update_cookies(cookies)
                return True

            # Not logged in - navigate to login page and wait for user
            await page.goto("https://www.instagram.com/accounts/login/")

            try:
                # Wait for navigation away from login page
                await page.wait_for_url(
                    lambda url: "instagram.com" in url and "login" not in url.lower(),
                    timeout=timeout * 1000,
                )

                await asyncio.sleep(3)

                # Get cookies and check for session
                cookies = await browser.get_cookies()
                session_cookie = next(
                    (c for c in cookies if c["name"] == "sessionid" and c.get("value")),
                    None,
                )

                if session_cookie:
                    self.update_cookies(cookies)
                    return True

            except TimeoutError:
                pass

        return False

    def get_instaloader_instance(
        self, authenticated: bool = True
    ) -> instaloader.Instaloader:
        """Create Instaloader instance, optionally with shared session."""
        logger.info(
            "session: creating instaloader instance authenticated=%s", authenticated
        )
        L = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
        )

        if authenticated:
            # Inject cookies from our session store
            cookies = self._session_store.export_for_instaloader()
            logger.info("session: injecting %s instaloader cookies", len(cookies))
            for name, value in cookies.items():
                L.context._session.cookies.set(name, value, domain=".instagram.com")

        return L

    def clear_session(self) -> None:
        """Clear all session data."""
        self._session_store.clear()

        # Also clear browser data
        if self._browser_data_dir.exists():
            shutil.rmtree(self._browser_data_dir)


class PersistentBrowserSession:
    """
    Persistent headful browser for search operations.

    Keeps a single browser window open across multiple operations,
    allowing the user to see what's happening and intervene if needed
    (e.g., solving captchas or challenges).
    """

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
        session_manager: SessionManager,
        browser_engine: str = "chrome",
        cdp_url: str | None = None,
    ):
        self._session_manager = session_manager
        self._browser_engine = browser_engine.lower()
        self._cdp_url = cdp_url
        self._browser_context: BrowserContextManager | None = None
        self._page = None
        self._started = False

    @property
    def browser_context(self) -> BrowserContextManager | None:
        """Get the underlying browser context manager."""
        return self._browser_context

    async def ensure_ready(self, check_login: bool = True) -> bool:
        """
        Start browser if needed and optionally ensure user is logged in.

        Returns:
            True if browser is ready (and logged in if check_login is True)
        """
        if not self._started:
            logger.info("session: starting browser")
            await self._start_browser()

        if not check_login:
            logger.info("session: skipping pre-search auth check")
            return True

        # Check if logged in by looking at cookies
        logger.info("session: checking logged in status")
        is_logged_in = await self._check_logged_in()

        if not is_logged_in:
            # Navigate to login page and wait for user
            logger.info("session: login required, waiting for user")
            return await self._wait_for_login()

        logger.info("session: already logged in")
        return True

    async def _start_browser(self) -> None:
        """Start a headed browser and import existing Instagram cookies."""
        if self._cdp_url:
            mode = BrowserMode.HEADED
            user_data_dir = None
        elif self._browser_engine == "camoufox":
            mode = BrowserMode.CAMOUFOX
            user_data_dir = self._session_manager.get_browser_data_dir("camoufox")
        else:
            mode = BrowserMode.HEADED
            user_data_dir = self._session_manager.get_browser_data_dir("chrome")

        logger.info(
            "session: browser engine=%s mode=%s cdp_url=%s",
            self._browser_engine,
            mode.name.lower(),
            self._cdp_url or "none",
        )
        config = BrowserConfig(
            mode=mode,
            humanize=True,
            user_data_dir=user_data_dir,
            cdp_url=self._cdp_url,
        )
        self._browser_context = BrowserContextManager(config)
        await self._browser_context.__aenter__()
        self._started = True

        bootstrap_cookies = self._session_manager.get_bootstrap_cookies()
        if bootstrap_cookies and not self._cdp_url:
            logger.info(
                "session: seeding browser with %s cookies", len(bootstrap_cookies)
            )
            await self._browser_context.add_cookies(bootstrap_cookies)

    async def _check_logged_in(self) -> bool:
        """Check if user is logged into Instagram."""
        if not self._browser_context:
            return False

        page = await self._browser_context.new_page()
        try:
            logger.info("session: navigating to instagram home")
            await page.goto("https://www.instagram.com/")
            await asyncio.sleep(2)

            # Check for session cookie
            cookies = await self._browser_context.get_cookies()
            session_cookie = next(
                (c for c in cookies if c["name"] == "sessionid" and c.get("value")),
                None,
            )

            # Also check URL - if redirected to login, not logged in
            current_url = page.url.lower()
            if "login" in current_url:
                logger.info("session: redirected to login page")
                return False

            if session_cookie:
                # Update stored cookies
                self._session_manager.update_cookies(cookies)
                logger.info("session: session cookie found, logged in")
                return True

            logger.info("session: no session cookie detected")
            return False
        finally:
            await page.close()

    async def _wait_for_login(self, timeout: int = 300) -> bool:
        """Wait for user to log in manually."""
        if not self._browser_context:
            return False

        page = await self._browser_context.new_page()
        try:
            logger.info("session: opening login page")
            await page.goto("https://www.instagram.com/accounts/login/")

            # Wait for navigation away from login page
            try:
                await page.wait_for_url(
                    lambda url: "instagram.com" in url and "login" not in url.lower(),
                    timeout=timeout * 1000,
                )
                await asyncio.sleep(3)

                # Get cookies and save
                cookies = await self._browser_context.get_cookies()
                session_cookie = next(
                    (c for c in cookies if c["name"] == "sessionid" and c.get("value")),
                    None,
                )

                if session_cookie:
                    self._session_manager.update_cookies(cookies)
                    logger.info("session: login successful, cookies saved")
                    return True

            except TimeoutError:
                logger.info("session: login timed out")
                pass

            return False
        finally:
            await page.close()

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

        # DOM-based detection (faster, fewer false positives)
        for selector in self.CHALLENGE_SELECTORS:
            try:
                if await page.query_selector(selector):
                    return True
            except Exception:
                continue

        return False

    async def wait_for_challenge_resolution(
        self,
        page,
        timeout: int = 300,
        on_challenge_detected: Callable[[], None] | None = None,
    ) -> bool:
        """
        Wait for user to solve a challenge in the browser.

        Args:
            page: Playwright page object
            timeout: Maximum time to wait in seconds
            on_challenge_detected: Optional callback when challenge is detected

        Returns:
            True if challenge was resolved, False if timeout
        """
        if on_challenge_detected:
            on_challenge_detected()

        start_time = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start_time < timeout:
            if not await self.check_for_challenge(page):
                # Challenge resolved
                await asyncio.sleep(2)  # Brief pause after resolution
                return True
            await asyncio.sleep(2)  # Poll every 2 seconds

        return False

    async def new_page(self):
        """Create a new page in the browser context."""
        if not self._browser_context:
            raise RuntimeError("Browser not started. Call ensure_ready() first.")
        return await self._browser_context.new_page()

    async def close(self) -> None:
        """Clean up and close the browser."""
        if not self._browser_context:
            return

        logger.info("session: closing browser context")
        try:
            await asyncio.wait_for(
                self._browser_context.__aexit__(None, None, None), timeout=10
            )
        except Exception as e:
            logger.warning("session: browser close failed error=%s", e)
        finally:
            self._browser_context = None
            self._started = False
