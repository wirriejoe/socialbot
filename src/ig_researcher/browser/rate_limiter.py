"""Rate limiting with exponential backoff and jitter."""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from ig_researcher.logging_utils import get_logger

logger = get_logger(__name__)

class RateLimitState(Enum):
    """Current state of rate limiting."""

    OK = "ok"
    SOFT_LIMIT = "soft_limit"  # Approaching limit
    HARD_LIMIT = "hard_limit"  # At limit, must wait
    BLOCKED = "blocked"  # Detected action block


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    # Per-hour limits (conservative)
    max_searches_per_hour: int = 20
    max_profile_views_per_hour: int = 50
    max_scroll_actions_per_hour: int = 100

    # Backoff configuration
    initial_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 300.0  # 5 minutes
    backoff_multiplier: float = 2.0
    jitter_factor: float = 0.3  # 30% jitter

    # Block detection
    max_consecutive_failures: int = 5
    block_cooldown_min_minutes: int = 30
    block_cooldown_max_minutes: int = 120


@dataclass
class ActionCounter:
    """Tracks action counts within a time window."""

    count: int = 0
    window_start: datetime = field(default_factory=datetime.now)
    window_duration: timedelta = field(default_factory=lambda: timedelta(hours=1))

    def increment(self) -> None:
        """Increment the counter, resetting window if needed."""
        self._maybe_reset_window()
        self.count += 1

    def get_count(self) -> int:
        """Get current count, resetting window if needed."""
        self._maybe_reset_window()
        return self.count

    def _maybe_reset_window(self) -> None:
        """Reset window if it has expired."""
        if datetime.now() - self.window_start > self.window_duration:
            self.count = 0
            self.window_start = datetime.now()


class RateLimiter:
    """
    Rate limiter with exponential backoff for Instagram operations.

    Features:
    - Per-action-type rate limiting
    - Exponential backoff with jitter
    - Automatic cooldown periods
    - Action block detection
    """

    def __init__(self, config: RateLimitConfig | None = None):
        self.config = config or RateLimitConfig()
        self._counters: dict[str, ActionCounter] = {}
        self._current_backoff: float = 0
        self._consecutive_failures: int = 0
        self._blocked_until: datetime | None = None

    def _get_counter(self, action_type: str) -> ActionCounter:
        """Get or create counter for action type."""
        if action_type not in self._counters:
            self._counters[action_type] = ActionCounter()
        return self._counters[action_type]

    def _get_limit(self, action_type: str) -> int:
        """Get the rate limit for an action type."""
        limits = {
            "search": self.config.max_searches_per_hour,
            "profile_view": self.config.max_profile_views_per_hour,
            "scroll": self.config.max_scroll_actions_per_hour,
        }
        return limits.get(action_type, 50)

    def check_state(self, action_type: str) -> RateLimitState:
        """Check current rate limit state for an action type."""
        if self._blocked_until and datetime.now() < self._blocked_until:
            return RateLimitState.BLOCKED

        counter = self._get_counter(action_type)
        limit = self._get_limit(action_type)
        current = counter.get_count()

        if current >= limit:
            return RateLimitState.HARD_LIMIT
        elif current >= limit * 0.8:
            return RateLimitState.SOFT_LIMIT
        return RateLimitState.OK

    async def wait_if_needed(self, action_type: str) -> None:
        """Wait if rate limited, with exponential backoff."""
        state = self.check_state(action_type)

        if state == RateLimitState.BLOCKED:
            wait_time = (self._blocked_until - datetime.now()).total_seconds()
            if wait_time > 0:
                logger.info(
                    "rate_limiter: blocked action=%s wait=%.1fs",
                    action_type,
                    wait_time,
                )
                await asyncio.sleep(wait_time)
            self._blocked_until = None
            return

        if state == RateLimitState.HARD_LIMIT:
            backoff = self._calculate_backoff()
            logger.info(
                "rate_limiter: hard limit action=%s backoff=%.1fs",
                action_type,
                backoff,
            )
            await asyncio.sleep(backoff)
            self._current_backoff = backoff

    def _calculate_backoff(self) -> float:
        """Calculate backoff time with exponential increase and jitter."""
        if self._current_backoff == 0:
            base = self.config.initial_backoff_seconds
        else:
            base = min(
                self._current_backoff * self.config.backoff_multiplier,
                self.config.max_backoff_seconds,
            )

        # Add jitter (random variation between -30% and +30%)
        jitter = base * self.config.jitter_factor * random.uniform(-1, 1)
        return max(0.1, base + jitter)

    def record_action(self, action_type: str) -> None:
        """Record a successful action."""
        self._get_counter(action_type).increment()
        self._consecutive_failures = 0
        self._current_backoff = 0

    def record_failure(self, action_type: str, is_rate_limit: bool = False) -> None:
        """Record a failed action."""
        self._consecutive_failures += 1

        if is_rate_limit or self._consecutive_failures >= 3:
            self._current_backoff = self._calculate_backoff()

        if self._consecutive_failures >= self.config.max_consecutive_failures:
            # Assume we're blocked, enter extended cooldown
            block_minutes = random.randint(
                self.config.block_cooldown_min_minutes,
                self.config.block_cooldown_max_minutes,
            )
            self._blocked_until = datetime.now() + timedelta(minutes=block_minutes)
            logger.info(
                "rate_limiter: block detected action=%s cooldown=%sm",
                action_type,
                block_minutes,
            )

    def reset(self) -> None:
        """Reset all rate limit state."""
        self._counters.clear()
        self._current_backoff = 0
        self._consecutive_failures = 0
        self._blocked_until = None

    def get_wait_time(self, action_type: str) -> float:
        """Get estimated wait time before next action is allowed."""
        state = self.check_state(action_type)

        if state == RateLimitState.BLOCKED and self._blocked_until:
            return max(0.0, (self._blocked_until - datetime.now()).total_seconds())
        elif state == RateLimitState.HARD_LIMIT:
            return self._calculate_backoff()
        return 0
