"""Paths and settings shared by the offline pipeline and the agents.

Every setting comes from the environment (or `.env`), never from code.
`REDFLAG_DATA_DIR` moves all data, which the tests use to work in a temp dir.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal `.env` loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _dotenv_value(value))


def _dotenv_value(raw: str) -> str:
    """A quoted value is taken verbatim; an unquoted one ends at ` #` (a comment)."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        return raw[1:end] if end > 0 else raw[1:]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


@dataclass(frozen=True)
class Settings:
    data: Path
    universe_from: date
    universe_to: date
    http_min_interval_s: float
    http_timeout_s: float

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def archive_db(self) -> Path:
        return self.data / "archive.db"

    @property
    def failures_csv(self) -> Path:
        return self.data / "failures.csv"

    @property
    def logs(self) -> Path:
        return Path(os.environ.get("REDFLAG_LOG_DIR", ROOT / "logs"))


def settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        data=Path(env("REDFLAG_DATA_DIR", ROOT / "data")),
        universe_from=date.fromisoformat(env("UNIVERSE_FROM", "2023-01-01")),
        # Up to today by default, so a refresh never leaves this year's IPOs out.
        universe_to=date.fromisoformat(env("UNIVERSE_TO", date.today().isoformat())),
        http_min_interval_s=float(env("HTTP_MIN_INTERVAL_S", "0.6")),
        http_timeout_s=float(env("HTTP_TIMEOUT_S", "30")),
    )
