"""pytest fixtures."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_path(tmp_path) -> str:
    return str(tmp_path / "energy.sqlite3")


@pytest.fixture()
def settings(db_path) -> Settings:
    return Settings(db_path=db_path, busy_timeout_ms=30_000)


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient

    with TestClient(create_app(settings)) as test_client:
        yield test_client
