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


def get_config() -> Config:
    return Config()
