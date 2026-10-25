"""Configuration from environment variables (optionally loaded from a .env file)."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "bmk"
LOG_DIR = Path.home() / "Library" / "Logs" / "bmk"

CLOUDTABS_CANDIDATES = [
    Path.home() / "Library/Containers/com.apple.Safari/Data/Library/Safari/CloudTabs.db",
    Path.home() / "Library/Safari/CloudTabs.db",
]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: Path(os.getenv("BMK_DB_PATH", APP_SUPPORT_DIR / "bookmarks.db")).expanduser())
    cloudtabs_path: Path | None = field(
        default_factory=lambda: Path(os.environ["BMK_CLOUDTABS_PATH"]).expanduser() if os.getenv("BMK_CLOUDTABS_PATH") else None
    )
    cloudtabs_devices: list[str] = field(default_factory=lambda: _env_list("BMK_CLOUDTABS_DEVICES"))
    cloudtabs_exclude_devices: list[str] = field(default_factory=lambda: _env_list("BMK_CLOUDTABS_EXCLUDE_DEVICES"))
    resurrect_on_new_save: bool = field(default_factory=lambda: _env_bool("BMK_RESURRECT_ON_NEW_SAVE", False))
    display_tz: str = field(default_factory=lambda: os.getenv("BMK_DISPLAY_TZ", "Asia/Singapore"))
    poll_interval: int = field(default_factory=lambda: int(os.getenv("BMK_POLL_INTERVAL", "1800")))
    port: int = field(default_factory=lambda: int(os.getenv("BMK_PORT", "8765")))
    # Enrichment (CG3): fetch pages for titles, summaries and text. Off means nothing is fetched.
    enrich: bool = field(default_factory=lambda: _env_bool("BMK_ENRICH", True))
    # Seconds of enrichment after each `bmk ingest` (and so after each background scan); 0 disables it.
    process_budget: int = field(default_factory=lambda: int(os.getenv("BMK_PROCESS_BUDGET", "300")))
    fetch_concurrency: int = field(default_factory=lambda: int(os.getenv("BMK_FETCH_CONCURRENCY", "8")))

    @property
    def token_path(self) -> Path:
        """API write token, kept next to the database (0600)."""
        return self.db_path.parent / "token"


def get_config() -> Config:
    return Config()
