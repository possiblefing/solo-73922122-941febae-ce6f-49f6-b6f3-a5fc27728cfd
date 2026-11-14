"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


@dataclass(frozen=True)
class Settings:
    """Settings for one API process.

    Both API processes point at the same SQLite file; all coordination happens
    inside SQLite (WAL + ``BEGIN IMMEDIATE`` write transactions).
    """

    db_path: str = "/data/energy.sqlite3"
    busy_timeout_ms: int = 30_000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("ENERGY_DB_PATH", "/data/energy.sqlite3"),
            busy_timeout_ms=_env_int("ENERGY_BUSY_TIMEOUT_MS", 30_000, 100),
        )
