"""Keychain helpers for storing API keys on macOS."""

from __future__ import annotations

import subprocess
import sys

SERVICE_NAME = "ig-researcher"
ACCOUNT_NAME = "gemini-api-key"


def _ensure_macos() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Keychain storage is only supported on macOS.")


def store_gemini_key(api_key: str) -> None:
    """Store the Gemini API key in macOS Keychain."""
    _ensure_macos()
    if not api_key:
        raise ValueError("API key is required.")
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-a",
            ACCOUNT_NAME,
            "-s",
            SERVICE_NAME,
            "-w",
            api_key,
            "-U",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def load_gemini_key() -> str | None:
    """Load the Gemini API key from macOS Keychain."""
    _ensure_macos()
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-a",
            ACCOUNT_NAME,
            "-s",
            SERVICE_NAME,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
