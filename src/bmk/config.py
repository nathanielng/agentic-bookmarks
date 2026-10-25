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
    # Local AI (CG6). Models are read from the Hugging Face cache; `bmk ai download` fetches them.
    # The LLM proposes the category list; the decision model (Jev-style) assigns categories.
    llm_model: str = field(default_factory=lambda: os.getenv("BMK_LLM_MODEL", "mlx-community/Qwen3.8-27B-4bit"))
    decider_repo: str = field(default_factory=lambda: os.getenv("BMK_DECIDER_REPO", "alibiserikbay/JevK5-GGUF"))
    decider_file: str = field(default_factory=lambda: os.getenv("BMK_DECIDER_FILE", "jevk5-9b-v0.3.3-Q5_K_M.gguf"))
    # Calibration temperatures belong to the model file (JevK5-GGUF card); change them with the file.
    decider_temperature: float = field(default_factory=lambda: float(os.getenv("BMK_DECIDER_TEMPERATURE", "1.316")))
    decider_knockout_temperature: float = field(default_factory=lambda: float(os.getenv("BMK_DECIDER_KNOCKOUT_TEMPERATURE", "1.05")))
    # An already running llama-server with the decision model; otherwise one is started on demand.
    decider_url: str | None = field(default_factory=lambda: os.getenv("BMK_DECIDER_URL") or None)
    decider_port: int = field(default_factory=lambda: int(os.getenv("BMK_DECIDER_PORT", "8091")))
    llama_server: str = field(default_factory=lambda: os.getenv("BMK_LLAMA_SERVER", "llama-server"))
    # MCP: read and propose tools are always on; single-item writes (add, edit, keep, star, trash) need this.
    mcp_allow_writes: bool = field(default_factory=lambda: _env_bool("BMK_MCP_ALLOW_WRITES", False))

    @property
    def token_path(self) -> Path:
        """API write token, kept next to the database (0600)."""
        return self.db_path.parent / "token"

    @property
    def categories_path(self) -> Path:
        """Your category list as JSON, kept next to the database (outside the repo) and rewritten on every change."""
        return Path(os.getenv("BMK_CATEGORIES_FILE", self.db_path.parent / "categories.json")).expanduser()


def get_config() -> Config:
    return Config()
