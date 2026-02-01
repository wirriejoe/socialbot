"""Configuration management using Pydantic Settings."""

from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load .env file before Settings class is instantiated
load_dotenv(find_dotenv(), override=True)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Other API Keys
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")

    # Data directory
    data_dir: Path = Field(
        default_factory=lambda: Path.home() / ".ig_researcher",
        alias="IG_RESEARCHER_DATA_DIR",
    )

    # Model configuration
    gemini_model: str = Field(default="gemini/gemini-2.5-flash")

    # Browser configuration
    browser_engine: str = Field(
        default="chrome",
        alias="IG_RESEARCHER_BROWSER_ENGINE",
    )
    chrome_cdp_url: str | None = Field(
        default=None,
        alias="IG_RESEARCHER_CHROME_CDP_URL",
    )

    # Logging configuration
    log_level: str = Field(
        default="INFO",
        alias="IG_RESEARCHER_LOG_LEVEL",
    )

    # Rate limiting defaults
    max_searches_per_hour: int = Field(default=20)
    max_videos_per_search: int = Field(
        default=20,
        alias="IG_RESEARCHER_MAX_VIDEOS_PER_SEARCH",
    )

    # Instaloader concurrency
    instaloader_concurrency: int = Field(
        default=3,
        alias="IG_RESEARCHER_INSTALOADER_CONCURRENCY",
    )
    instaloader_authenticated: bool = Field(
        default=False,
        alias="IG_RESEARCHER_INSTALOADER_AUTH",
    )

    # Video analysis concurrency
    analysis_concurrency: int = Field(
        default=3,
        alias="IG_RESEARCHER_ANALYSIS_CONCURRENCY",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def profiles_dir(self) -> Path:
        """Directory for browser profiles and sessions."""
        path = self.data_dir / "profiles"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cache_dir(self) -> Path:
        """Directory for cached results."""
        path = self.data_dir / "cache"
        path.mkdir(parents=True, exist_ok=True)
        return path



# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
