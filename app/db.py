"""SQLite access layer.

Design notes
------------
* The database is shared by several API processes.  Every state-changing
  operation runs in a single ``BEGIN IMMEDIATE`` transaction, so SQLite's
  write lock serialises writers across processes.
* Readings and the derived intervals are written in that same transaction.
  A crash therefore either keeps the complete new state or the complete old
  state, never a mixture.
* Readers use an explicit deferred transaction.  In WAL mode a reader sees a
  stable snapshot that is unaffected by concurrent writers, so the readings,
  intervals and break points returned by one query always belong to the same
  committed state.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from .config import Settings

SCHEMA_VERSION = 1


class DatabaseBusy(RuntimeError):
    """The database stayed locked longer than the configured timeout."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_record_id  TEXT    NOT NULL,
    meter_id          TEXT    NOT NULL,
    read_at_us        INTEGER NOT NULL,
    reading           INTEGER NOT NULL CHECK (reading >= 0),
    meter_replaced    INTEGER NOT NULL CHECK (meter_replaced IN (0, 1)),
    content_hash      TEXT    NOT NULL,
    created_commit_seq INTEGER NOT NULL,
    UNIQUE (source_record_id),
    UNIQUE (meter_id, read_at_us)
);

CREATE TABLE IF NOT EXISTS intervals (
    id            INTEGER PRIMARY KEY,
    meter_id      TEXT    NOT NULL,
    from_reading_id INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    to_reading_id   INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    from_at_us    INTEGER NOT NULL,
    to_at_us      INTEGER NOT NULL,
    from_value    INTEGER NOT NULL,
    to_value      INTEGER NOT NULL,
    usage         INTEGER,
    is_break      INTEGER NOT NULL CHECK (is_break IN (0, 1)),
    break_reason  TEXT,
    commit_seq    INTEGER NOT NULL,
    UNIQUE (meter_id, to_reading_id),
    CHECK (
        (is_break = 0 AND usage IS NOT NULL AND break_reason IS NULL)
        OR
        (is_break = 1 AND usage IS NULL
         AND break_reason IN ('meter_replaced', 'counter_rollback'))
    )
);

CREATE INDEX IF NOT EXISTS idx_intervals_meter_to
    ON intervals (meter_id, to_at_us);

CREATE INDEX IF NOT EXISTS idx_intervals_from
    ON intervals (from_reading_id);

CREATE INDEX IF NOT EXISTS idx_intervals_to
    ON intervals (to_reading_id);
"""


def _is_lock_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def connect(settings: Settings) -> sqlite3.Connection:
    """Open one connection.  Callers must close it."""
    directory = os.path.dirname(os.path.abspath(settings.db_path))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.busy_timeout_ms / 1000.0,
        isolation_level=None,  # explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(settings.busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    return conn


@contextmanager
def connection(settings: Settings) -> Iterator[sqlite3.Connection]:
    conn = connect(settings)
    try:
        yield conn
    finally:
        conn.close()


def initialize(settings: Settings) -> None:
    """Create the schema and switch the file to WAL mode.

    Safe to call from several processes at once: lock contention is retried
    with backoff until the configured busy timeout expires.
    """
    deadline = time.monotonic() + settings.busy_timeout_ms / 1000.0
    for attempt in range(1, 10_000):
        try:
            with connection(settings) as conn:
                # journal_mode cannot be changed inside a transaction.
                conn.execute("PRAGMA journal_mode = WAL")
                with write_transaction(conn):
                    for statement in _SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    conn.execute(
                        "INSERT OR IGNORE INTO meta (key, value) "
                        "VALUES ('commit_seq', '0')"
                    )
                    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
                    if user_version not in (0, SCHEMA_VERSION):
                        raise RuntimeError(
                            f"unsupported schema version {user_version}"
                        )
                    # user_version is a header value; it persists with COMMIT.
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise DatabaseBusy(
                    "database is locked while initialising"
                ) from exc
            time.sleep(min(0.05 * attempt, 0.5))


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialise a complete state change behind SQLite's write lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Expose one immutable database snapshot to all reads in the block."""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def current_commit_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'commit_seq'"
    ).fetchone()
    if row is None:
        raise RuntimeError("database is not initialised")
    return int(row["value"])


def bump_commit_seq(conn: sqlite3.Connection) -> int:
    seq = current_commit_seq(conn) + 1
    conn.execute(
        "UPDATE meta SET value = ? WHERE key = 'commit_seq'", (str(seq),)
    )
    return seq
