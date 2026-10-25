from __future__ import annotations

from datetime import date

import pytest

from redflag.common.db import connect
from redflag.common.http import Fetcher
from redflag.common.log import StageLog
from redflag.config import Settings
from redflag.pipeline.base import StageContext


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("REDFLAG_LOG_DIR", str(tmp_path / "logs"))
    return Settings(data=tmp_path / "data", universe_from=date(2023, 1, 1),
                    universe_to=date(2025, 12, 31), http_min_interval_s=0,
                    http_timeout_s=5)


@pytest.fixture
def conn(cfg):
    c = connect(cfg.archive_db)
    yield c
    c.close()


@pytest.fixture
def ctx(cfg, conn) -> StageContext:
    log = StageLog("test", cfg.logs, cfg.failures_csv)
    return StageContext(cfg, conn, Fetcher(0, 5, retries=0), log)
