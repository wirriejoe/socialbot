"""Logging helpers."""

from __future__ import annotations

import logging
from typing import Optional

_LOGGER_ROOT = "ig_researcher"


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a logger scoped under the ig_researcher root."""
    if not name:
        return logging.getLogger(_LOGGER_ROOT)
    if name.startswith(_LOGGER_ROOT):
        return logging.getLogger(name)
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")
