"""Encrypted session storage for Instagram cookies."""

import json
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SessionStore:
    """
    Encrypted storage for Instagram session cookies.

    Uses Fernet symmetric encryption to protect sensitive session data at rest.
    The encryption key is generated once and stored in the data directory.
    """

    def __init__(self, data_dir: Path, profile_name: str = "default"):
        """
        Initialize session store.

        Args:
            data_dir: Base data directory for the application
            profile_name: Name of the profile (allows multiple accounts)
        """
        self.data_dir = data_dir
        self.profile_name = profile_name
        self.profile_dir = data_dir / "profiles" / profile_name
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self._cookies_file = self.profile_dir / "cookies.enc"
        self._key = self._get_or_create_key()
        self._fernet = Fernet(self._key)

    def _get_or_create_key(self) -> bytes:
        """Get or generate the encryption key."""
        key_file = self.data_dir / ".key"

        if key_file.exists():
            return key_file.read_bytes()

        # Generate new key
        key = Fernet.generate_key()
        key_file.write_bytes(key)
        key_file.chmod(0o600)  # Restrict permissions
        return key

    def save_cookies(self, cookies: dict[str, str]) -> None:
        """
        Encrypt and save cookies to disk.

        Args:
            cookies: Dictionary of cookie name -> value pairs
        """
        # Add metadata
        cookies["_updated"] = datetime.now().isoformat()

        # Encrypt and save
        data = json.dumps(cookies).encode()
        encrypted = self._fernet.encrypt(data)
        self._cookies_file.write_bytes(encrypted)
        self._cookies_file.chmod(0o600)

    def load_cookies(self) -> dict[str, str]:
        """
        Load and decrypt cookies from disk.

        Returns:
            Dictionary of cookie name -> value pairs, or empty dict if none exist
        """
        if not self._cookies_file.exists():
            return {}

        try:
            encrypted = self._cookies_file.read_bytes()
            decrypted = self._fernet.decrypt(encrypted)
            return json.loads(decrypted)
        except (InvalidToken, json.JSONDecodeError):
            # Corrupted or invalid data
            return {}

    def update_cookies(self, new_cookies: dict[str, str]) -> None:
        """
        Merge new cookies with existing ones.

        Args:
            new_cookies: New cookies to add/update
        """
        existing = self.load_cookies()
        existing.update(new_cookies)
        self.save_cookies(existing)

    def update_from_browser(self, browser_cookies: list[dict]) -> None:
        """
        Update stored cookies from Playwright cookie export.

        Filters to only include Instagram cookies.

        Args:
            browser_cookies: List of cookie dicts from context.cookies()
        """
        cookie_dict = {}
        for cookie in browser_cookies:
            domain = cookie.get("domain", "")
            if "instagram.com" in domain:
                cookie_dict[cookie["name"]] = cookie["value"]

        if cookie_dict:
            self.update_cookies(cookie_dict)

    def clear(self) -> None:
        """Delete all stored session data."""
        if self._cookies_file.exists():
            self._cookies_file.unlink()

    @property
    def has_session(self) -> bool:
        """Check if a session exists (has sessionid cookie)."""
        cookies = self.load_cookies()
        return bool(cookies.get("sessionid"))

    def get_last_updated(self) -> datetime | None:
        """Get the last update timestamp."""
        cookies = self.load_cookies()
        updated = cookies.get("_updated")
        if updated:
            try:
                return datetime.fromisoformat(updated)
            except ValueError:
                pass
        return None

    def export_for_instaloader(self) -> dict[str, str]:
        """
        Export cookies in format suitable for Instaloader.

        Returns:
            Dict of cookie names to values for the required cookies
        """
        required = ["sessionid", "csrftoken", "ds_user_id", "mid", "ig_did", "rur"]
        cookies = self.load_cookies()
        return {name: cookies[name] for name in required if name in cookies}
