"""Human-like behavior patterns for browser automation."""

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Protocol


class Page(Protocol):
    """Protocol for Playwright Page."""

    async def evaluate(self, expression: str) -> Any: ...
    async def query_selector(self, selector: str) -> Any: ...


@dataclass
class HumanizationConfig:
    """Configuration for human-like behavior."""

    # Fast mode - skip delays for headful browser where user can see actions
    fast_mode: bool = False

    # Delay ranges (in seconds)
    min_action_delay: float = 0.5
    max_action_delay: float = 2.0
    min_scroll_delay: float = 1.0
    max_scroll_delay: float = 3.0
    min_typing_delay: float = 0.05
    max_typing_delay: float = 0.15

    # Scroll behavior
    scroll_variance: float = 0.2  # 20% variance in scroll distance
    scroll_pause_chance: float = 0.3  # 30% chance to pause mid-scroll

    # Session behavior
    min_session_actions: int = 5
    max_session_actions: int = 20
    break_duration_min: float = 30.0
    break_duration_max: float = 120.0


class HumanBehavior:
    """
    Implements human-like interaction patterns to avoid bot detection.

    Features:
    - Random delays with gaussian distribution
    - Natural scrolling with variable speeds
    - Reading time simulation
    - Session activity limits with breaks
    """

    def __init__(self, config: HumanizationConfig | None = None):
        self.config = config or HumanizationConfig()
        self._action_count = 0
        self._session_limit = random.randint(
            self.config.min_session_actions,
            self.config.max_session_actions,
        )

    async def random_delay(
        self,
        min_seconds: float | None = None,
        max_seconds: float | None = None,
    ) -> None:
        """Wait for a random human-like duration."""
        if self.config.fast_mode:
            await asyncio.sleep(0.1)  # Minimal delay in fast mode
            return

        min_s = min_seconds or self.config.min_action_delay
        max_s = max_seconds or self.config.max_action_delay

        # Use gaussian distribution centered on midpoint
        mean = (min_s + max_s) / 2
        std = (max_s - min_s) / 4
        delay = max(min_s, min(max_s, random.gauss(mean, std)))

        await asyncio.sleep(delay)

    async def simulate_reading_time(self, content_length: int) -> None:
        """
        Simulate time spent reading content.

        Assumes ~200 words per minute reading speed with variance.
        """
        words = content_length / 5  # Rough word estimate
        minutes = words / 200
        variance = random.uniform(0.8, 1.2)
        await asyncio.sleep(minutes * 60 * variance)

    async def human_scroll(
        self,
        page: Page,
        direction: str = "down",
        distance: int | None = None,
    ) -> None:
        """
        Perform human-like scrolling with variable speed and pauses.

        Args:
            page: Playwright page instance
            direction: "up" or "down"
            distance: Scroll distance in pixels (randomized if None)
        """
        viewport_height = await page.evaluate("window.innerHeight")

        if distance is None:
            # Scroll roughly one viewport with variance
            base_distance = viewport_height * 0.7
            variance = self.config.scroll_variance
            distance = int(base_distance * random.uniform(1 - variance, 1 + variance))

        if direction == "up":
            distance = -distance

        if self.config.fast_mode:
            # Fast mode: single scroll, minimal delay
            await page.evaluate(f"window.scrollBy(0, {distance})")
            await asyncio.sleep(0.2)
            return

        # Break scroll into smaller increments for realism
        increments = random.randint(3, 7)
        increment_distance = distance // increments

        for i in range(increments):
            await page.evaluate(f"window.scrollBy(0, {increment_distance})")

            # Variable delay between increments
            delay = random.uniform(0.05, 0.15)
            await asyncio.sleep(delay)

            # Occasional pause to "read"
            if random.random() < self.config.scroll_pause_chance:
                await self.random_delay(0.5, 1.5)

        # Final settling delay
        await self.random_delay(
            self.config.min_scroll_delay,
            self.config.max_scroll_delay,
        )

    async def scroll_to_load_more(
        self,
        page: Page,
        target_count: int,
        count_selector: str,
        max_scrolls: int = 20,
    ) -> int:
        """
        Scroll to load dynamic content until target count reached.

        Returns actual count of items loaded.
        """
        current_count = 0
        no_change_count = 0

        for _ in range(max_scrolls):
            # Get current item count
            new_count = await page.evaluate(
                f"document.querySelectorAll('{count_selector}').length"
            )

            if new_count >= target_count:
                return new_count

            if new_count == current_count:
                no_change_count += 1
                if no_change_count >= 3:
                    # No new content loading, probably at end
                    return new_count
            else:
                current_count = new_count
                no_change_count = 0

            await self.human_scroll(page)

            # Check session limits
            await self._check_session_limit()

        return current_count

    async def _check_session_limit(self) -> None:
        """Check if we've hit session action limit and need a break."""
        if self.config.fast_mode:
            return  # Skip session breaks in fast mode

        self._action_count += 1

        if self._action_count >= self._session_limit:
            # Take a short break
            break_time = random.uniform(
                self.config.break_duration_min,
                self.config.break_duration_max,
            )
            await asyncio.sleep(break_time)

            # Reset for next session
            self._action_count = 0
            self._session_limit = random.randint(
                self.config.min_session_actions,
                self.config.max_session_actions,
            )

    async def human_type(self, page: Page, selector: str, text: str) -> None:
        """Type text with human-like keystroke timing."""
        element = await page.query_selector(selector)
        if not element:
            raise ValueError(f"Element not found: {selector}")

        await element.click()
        await self.random_delay(0.2, 0.5)

        for char in text:
            delay_ms = random.uniform(
                self.config.min_typing_delay * 1000,
                self.config.max_typing_delay * 1000,
            )
            await element.type(char, delay=delay_ms)

            # Occasional longer pause (thinking)
            if random.random() < 0.1:
                await self.random_delay(0.3, 0.7)
