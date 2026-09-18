"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

VALID_ANCHORS = ("main_card", "prelims")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _guild_ids(name: str) -> list[int]:
    raw = os.getenv(name, "")
    ids = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    return ids


@dataclass(slots=True)
class Config:
    token: str
    dev_guild_ids: list[int] = field(default_factory=list)
    database_path: str = "ufcbot.sqlite3"
    sync_interval_minutes: int = 180
    default_days_ahead: int = 60
    default_duration_minutes: int = 240
    default_start_anchor: str = "main_card"
    enable_poster_art: bool = True
    cache_ttl_seconds: int = 900
    log_level: str = "INFO"
    enable_predictions: bool = True
    data_dir: str = "data"
    model_dir: str = "models"
    stats_refresh_hours: int = 24

    @classmethod
    def load(cls) -> Config:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
            )

        anchor = os.getenv("DEFAULT_START_ANCHOR", "main_card").strip().lower()
        if anchor not in VALID_ANCHORS:
            anchor = "main_card"

        return cls(
            token=token,
            dev_guild_ids=_guild_ids("DEV_GUILD_IDS"),
            database_path=os.getenv("DATABASE_PATH", "ufcbot.sqlite3").strip() or "ufcbot.sqlite3",
            sync_interval_minutes=max(15, _int("SYNC_INTERVAL_MINUTES", 180)),
            default_days_ahead=max(1, min(365, _int("DEFAULT_DAYS_AHEAD", 60))),
            default_duration_minutes=max(30, min(1440, _int("DEFAULT_EVENT_DURATION_MINUTES", 240))),
            default_start_anchor=anchor,
            enable_poster_art=_bool("ENABLE_POSTER_ART", True),
            cache_ttl_seconds=max(60, _int("CACHE_TTL_SECONDS", 900)),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            enable_predictions=_bool("ENABLE_PREDICTIONS", True),
            data_dir=os.getenv("DATA_DIR", "data").strip() or "data",
            model_dir=os.getenv("MODEL_DIR", "models").strip() or "models",
            stats_refresh_hours=max(1, _int("STATS_REFRESH_HOURS", 24)),
        )
