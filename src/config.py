"""Loads config/config.yaml + .env into a single Config object.

Notification-channel credentials are intentionally NOT loaded here — each
channel only requires its own env vars when it's actually listed in
notifications.channels, and that decision belongs to
notifiers.build_channels(), not this module.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = "config/config.yaml"
DEFAULT_CHANNELS = ["telegram"]


@dataclass
class Config:
    raw: dict[str, Any]
    db_path: str

    @property
    def sites(self) -> dict[str, Any]:
        return self.raw.get("sites", {})

    @property
    def matching(self) -> dict[str, Any]:
        return self.raw.get("matching", {})

    @property
    def polling(self) -> dict[str, Any]:
        return self.raw.get("polling", {})

    @property
    def anti_blocking(self) -> dict[str, Any]:
        return self.raw.get("anti_blocking", {})

    @property
    def notification_channels(self) -> list[str]:
        return self.raw.get("notifications", {}).get("channels", DEFAULT_CHANNELS)

    @property
    def telegram_config(self) -> dict[str, Any]:
        return self.raw.get("telegram", {})

    @property
    def whatsapp_config(self) -> dict[str, Any]:
        return self.raw.get("whatsapp", {})

    @property
    def summary_config(self) -> dict[str, Any]:
        return self.raw.get("summary", {})

    @property
    def agent(self) -> dict[str, Any]:
        """agent.yaml's `agent:` block, with env vars taking precedence --
        same precedence pattern as db_path/CONFIG_PATH above. Env vars are
        how k8s (agent.yaml, cronjob.yaml) wires in-cluster DNS URLs without
        duplicating them in config.yaml."""
        raw_agent = self.raw.get("agent", {})
        return {
            "ollama_base_url": os.environ.get("OLLAMA_BASE_URL") or raw_agent.get("ollama_base_url", "http://localhost:11434"),
            "ollama_model": os.environ.get("OLLAMA_MODEL") or raw_agent.get("ollama_model", "qwen3:8b"),
            "searxng_base_url": os.environ.get("SEARXNG_BASE_URL") or raw_agent.get("searxng_base_url", "http://localhost:8080"),
            "scoring_timeout_seconds": int(os.environ.get("SCORING_TIMEOUT_SECONDS") or raw_agent.get("scoring_timeout_seconds", 180)),
            "max_tool_calls_per_cycle": int(os.environ.get("MAX_TOOL_CALLS_PER_CYCLE") or raw_agent.get("max_tool_calls_per_cycle", 15)),
            "cycle_sleep_minutes": float(os.environ.get("CYCLE_SLEEP_MINUTES") or raw_agent.get("cycle_sleep_minutes", 7)),
            "min_relevance_score": int(os.environ.get("MIN_RELEVANCE_SCORE") or raw_agent.get("min_relevance_score", 6)),
            "reminder_offsets_minutes": list(raw_agent.get("reminder_offsets_minutes", [30, 60, 120])),
        }

    @property
    def logging_level(self) -> str:
        return self.raw.get("logging", {}).get("level", "INFO")

    def enabled_sites(self) -> dict[str, Any]:
        return {name: cfg for name, cfg in self.sites.items() if cfg.get("enabled")}


def _seed_from_bundled_default(path: Path) -> None:
    """In k8s, CONFIG_PATH points at a writable PVC path (so the dashboard can
    edit it) that's separate from the image's baked-in default. On first run
    the PVC path won't exist yet -- seed it from the image's default so both
    the scraper and dashboard containers work without a manual copy step.
    No-op for local/docker-compose runs, where CONFIG_PATH already points at
    the same file as the default."""
    default_path = Path(DEFAULT_CONFIG_PATH)
    if path.exists() or not default_path.exists() or default_path.resolve() == path.resolve():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(default_path, path)


def load_config(config_path: str | None = None, env_path: str | None = None) -> Config:
    load_dotenv(dotenv_path=env_path or ".env")

    path = Path(config_path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    _seed_from_bundled_default(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    db_path = os.environ.get("DB_PATH") or raw.get("database", {}).get("path", "data/jobbot.sqlite3")

    return Config(raw=raw, db_path=db_path)
