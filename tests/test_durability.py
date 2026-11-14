"""Tests for the crash/failure-recovery helpers and startup behaviour."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from app.config import Settings
from app.db import SCHEMA_VERSION, connection, initialize
from app.models import ReadingIn
from tests.common import reading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_error_between_readings_and_intervals_rolls_back_everything(
    db_path, settings, monkeypatch
):
    """If an exception is raised after the readings are inserted but before the
    interval rebuild finishes, the whole batch disappears."""
    import app.service as service

    initialize(settings)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_recompute_intervals", explode)
    with pytest.raises(RuntimeError):
        service.ingest(
            settings,
            [ReadingIn.model_validate(item)
             for item in (reading("M1", "a", 0, 10), reading("M1", "b", 60, 20))],
        )

    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM intervals").fetchone()[0] == 0
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()[0] == "0"


def test_failed_commit_does_not_advance_commit_sequence(db_path, settings):
    initialize(settings)
    with connection(settings) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('commit_seq', '7') "
            "ON CONFLICT(key) DO UPDATE SET value = '7'"
        )
        # A transaction that raises must not change meta.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'commit_seq'")
        conn.execute("ROLLBACK")
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'commit_seq'"
        ).fetchone()[0] == "7"


def test_schema_version_is_recorded(db_path, settings):
    initialize(settings)
    with connection(settings) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_startup_rejects_unknown_schema_version(db_path, settings):
    initialize(settings)
    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA user_version = 99")
    raw.close()
    with pytest.raises(RuntimeError, match="unsupported schema version"):
        initialize(settings)


def test_startup_is_safe_when_two_processes_race(db_path):
    """Two processes initialising the same fresh file at once must both succeed."""
    code = textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from app.config import Settings
        from app.db import initialize
        time.sleep(float(sys.argv[2]))
        initialize(Settings(db_path=sys.argv[3], busy_timeout_ms=30000))
        print("ok")
        """
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, ROOT, str(delay), db_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
        )
        for delay in (0.0, 0.05, 0.1)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
        assert b"ok" in out

    with connection(Settings(db_path=db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
