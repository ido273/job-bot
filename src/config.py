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
