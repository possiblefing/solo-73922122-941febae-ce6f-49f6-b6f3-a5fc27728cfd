"""Shared test helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def at(minutes: int) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def reading(
    meter: str,
    source: str,
    minute: int,
    value: int,
    replaced: bool = False,
) -> dict:
    return {
        "meter_id": meter,
        "source_record_id": source,
        "read_at": at(minute),
        "reading": value,
        "meter_replaced": replaced,
    }


def assert_meter_consistent(payload: dict) -> None:
    """Structural check: intervals must be a pure function of the readings.

    This is the invariant that a torn transaction or a mid-batch crash would
    violate.
    """
    readings = payload["readings"]
    intervals = payload["intervals"]
    assert payload["breaks"] == [i for i in intervals if i["is_break"]]
    assert len(intervals) == max(0, len(readings) - 1)

    previous = None
    for row in readings:
        if previous is not None:
            assert previous["read_at"] <= row["read_at"], "readings not sorted"
        previous = row

    for index, interval in enumerate(intervals):
        left, right = readings[index], readings[index + 1]
        assert interval["from_source_record_id"] == left["source_record_id"]
        assert interval["to_source_record_id"] == right["source_record_id"]
        assert interval["from_reading"] == left["reading"]
        assert interval["to_reading"] == right["reading"]
        if right["meter_replaced"]:
            assert interval["is_break"] is True
            assert interval["break_reason"] == "meter_replaced"
        elif right["reading"] < left["reading"]:
            assert interval["is_break"] is True
            assert interval["break_reason"] == "counter_rollback"
        else:
            assert interval["is_break"] is False
            assert interval["break_reason"] is None
            assert interval["usage"] == right["reading"] - left["reading"]
        if interval["is_break"]:
            assert interval["usage"] is None
            assert interval["break_reason"] in ("meter_replaced", "counter_rollback")
    expected_total = sum(
        i["usage"] for i in intervals if i["usage"] is not None
    )
    assert payload["usage_total"] == expected_total


def stored_intervals(conn: sqlite3.Connection, meter_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM intervals WHERE meter_id = ? ORDER BY to_at_us",
        (meter_id,),
    ).fetchall()


def assert_db_invariants(conn: sqlite3.Connection, meter_id: str) -> None:
    """Compare stored intervals with an independent recomputation."""
    rows = conn.execute(
        "SELECT s.source_record_id, s.read_at_us, s.reading, s.meter_replaced "
        "FROM readings s WHERE s.meter_id = ? ORDER BY s.read_at_us",
        (meter_id,),
    ).fetchall()
    stored = stored_intervals(conn, meter_id)
    assert len(stored) == max(0, len(rows) - 1)
    for left, right, interval in zip(rows, rows[1:], stored):
        expected_break = bool(right["meter_replaced"]) or (
            right["reading"] < left["reading"]
        )
        assert bool(interval["is_break"]) is expected_break
        if expected_break:
            assert interval["usage"] is None
            assert interval["break_reason"] in (
                "meter_replaced",
                "counter_rollback",
            )
        else:
            assert interval["usage"] == right["reading"] - left["reading"]
            assert interval["break_reason"] is None
        assert interval["from_at_us"] == left["read_at_us"]
        assert interval["to_at_us"] == right["read_at_us"]


def assert_usage_matches_readings(payload: dict) -> None:
    """Every interval must be derivable from the snapshot's own readings."""
    rows = payload["readings"]
    for index, interval in enumerate(payload["intervals"]):
        left, right = rows[index], rows[index + 1]
        if not interval["is_break"]:
            assert interval["usage"] == right["reading"] - left["reading"]
