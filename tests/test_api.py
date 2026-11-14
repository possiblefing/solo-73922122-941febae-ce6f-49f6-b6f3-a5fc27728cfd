"""Functional tests for ingest, undo and query semantics."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import connection
from tests.common import assert_db_invariants, assert_meter_consistent, at, reading


def post(client, readings):
    return client.post("/api/v1/readings", json={"readings": readings})


def undo(client, ids):
    return client.post("/api/v1/readings/undo", json={"source_record_ids": ids})


def get(client, meter, **params):
    return client.get(f"/api/v1/meters/{meter}", params=params)


def test_adjacent_differences(client):
    resp = post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 160),
            reading("M1", "c", 120, 250),
        ],
    )
    assert resp.status_code == 200
    body = get(client, "M1").json()
    assert [i["usage"] for i in body["intervals"]] == [60, 90]
    assert body["usage_total"] == 150
    assert body["breaks"] == []
    assert_meter_consistent(body)


def test_out_of_order_backfill_recomputes_intervals(client):
    post(client, [reading("M1", "a", 0, 100), reading("M1", "c", 120, 250)])
    assert [i["usage"] for i in get(client, "M1").json()["intervals"]] == [150]

    resp = post(client, [reading("M1", "b", 60, 160)])
    assert resp.status_code == 200
    assert resp.json()["changed"] is True
    body = get(client, "M1").json()
    assert [i["usage"] for i in body["intervals"]] == [60, 90]
    assert_meter_consistent(body)


def test_out_of_order_within_one_batch(client):
    post(
        client,
        [
            reading("M1", "c", 120, 250),
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 160),
        ],
    )
    body = get(client, "M1").json()
    assert [r["source_record_id"] for r in body["readings"]] == ["a", "b", "c"]
    assert [i["usage"] for i in body["intervals"]] == [60, 90]


def test_meter_replacement_creates_break_and_skips_usage(client):
    post(
        client,
        [
            reading("M1", "a", 0, 1000),
            reading("M1", "b", 60, 1200),
            reading("M1", "c", 120, 30, replaced=True),
            reading("M1", "d", 180, 80),
        ],
    )
    body = get(client, "M1").json()
    assert [i["usage"] for i in body["intervals"]] == [200, None, 50]
    assert [i["is_break"] for i in body["intervals"]] == [False, True, False]
    assert body["breaks"][0]["break_reason"] == "meter_replaced"
    assert body["usage_total"] == 250
    assert_meter_consistent(body)


def test_counter_rollback_creates_break_and_skips_usage(client):
    post(
        client,
        [
            reading("M1", "a", 0, 1000),
            reading("M1", "b", 60, 900),
            reading("M1", "c", 120, 950),
        ],
    )
    body = get(client, "M1").json()
    assert [i["usage"] for i in body["intervals"]] == [None, 50]
    assert body["breaks"][0]["break_reason"] == "counter_rollback"
    assert_meter_consistent(body)


def test_replacement_flag_on_later_insert_retroactively_breaks(client):
    post(client, [reading("M1", "a", 0, 10), reading("M1", "c", 120, 40)])
    assert [i["usage"] for i in get(client, "M1").json()["intervals"]] == [30]

    post(client, [reading("M1", "b", 60, 20, replaced=True)])
    body = get(client, "M1").json()
    assert [i["break_reason"] for i in body["intervals"]] == [
        "meter_replaced",
        None,
    ]
    assert [i["usage"] for i in body["intervals"]] == [None, 20]


def test_identical_retry_is_idempotent_and_does_not_recompute(client):
    batch = [
        reading("M1", "a", 0, 10),
        reading("M1", "b", 60, 20),
        reading("M2", "c", 0, 5),
    ]
    first = post(client, batch).json()
    assert first["counts"] == {"created": 3}

    second = post(client, batch)
    assert second.status_code == 200
    body = second.json()
    assert body["changed"] is False
    assert body["counts"] == {"unchanged": 3}
    assert body["commit_seq"] == first["commit_seq"]
    assert get(client, "M1").json()["commit_seq"] == first["commit_seq"]


def test_partial_retry_inserts_only_new_records(client):
    post(client, [reading("M1", "a", 0, 10)])
    resp = post(client, [reading("M1", "a", 0, 10), reading("M1", "b", 60, 25)])
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"unchanged": 1, "created": 1}
    assert [i["usage"] for i in get(client, "M1").json()["intervals"]] == [15]


def test_same_source_different_content_rejects_whole_batch(client):
    post(client, [reading("M1", "a", 0, 10)])

    resp = post(
        client,
        [
            reading("M2", "x", 0, 1),
            reading("M1", "a", 0, 11),  # same id, different value
        ],
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "batch_rejected"
    assert [c["type"] for c in body["conflicts"]] == ["source_record_conflict"]

    # Nothing from the rejected batch was written.
    assert get(client, "M2").status_code == 404
    assert get(client, "M1").json()["readings"][0]["reading"] == 10


def test_same_meter_same_time_rejects_whole_batch(client):
    post(client, [reading("M1", "a", 0, 10)])
    resp = post(
        client,
        [reading("M2", "y", 0, 1), reading("M1", "z", 0, 10)],
    )
    assert resp.status_code == 409
    assert resp.json()["conflicts"][0]["type"] == "meter_time_conflict"
    assert resp.json()["conflicts"][0]["conflicting_source_record_id"] == "a"
    assert get(client, "M2").status_code == 404


def test_duplicate_ids_inside_one_batch(client):
    same = post(
        client,
        [reading("M1", "a", 0, 10), reading("M1", "a", 0, 10)],
    )
    assert same.status_code == 200
    assert same.json()["counts"] == {"created": 1}

    conflicting = post(
        client,
        [reading("M1", "b", 0, 10), reading("M1", "b", 0, 11)],
    )
    assert conflicting.status_code == 409
    assert get(client, "M1").json()["readings"][-1]["source_record_id"] == "a"

    same_slot = post(
        client,
        [reading("M1", "c", 60, 10), reading("M1", "d", 60, 10)],
    )
    assert same_slot.status_code == 409
    assert same_slot.json()["conflicts"][0]["type"] == "meter_time_conflict"


def test_undo_reconnects_neighbours(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 160),
            reading("M1", "c", 120, 250),
        ],
    )
    resp = undo(client, ["b"])
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"removed": 1}
    assert resp.json()["affected_meters"][0]["usage_total"] == 150

    body = get(client, "M1").json()
    assert [r["source_record_id"] for r in body["readings"]] == ["a", "c"]
    assert [(i["from_source_record_id"], i["to_source_record_id"]) for i in body["intervals"]] == [
        ("a", "c")
    ]
    assert body["intervals"][0]["usage"] == 150
    assert_meter_consistent(body)


def test_undo_that_removes_a_break(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 20, replaced=True),
            reading("M1", "c", 120, 80),
        ],
    )
    assert len(get(client, "M1").json()["breaks"]) == 1
    undo(client, ["b"])
    body = get(client, "M1").json()
    assert len(body["breaks"]) == 1
    assert [i["usage"] for i in body["intervals"]] == [None]  # 100 -> 80 rollback
    assert body["intervals"][0]["break_reason"] == "counter_rollback"
    assert_meter_consistent(body)


def test_undo_missing_and_empty_meter(client):
    resp = undo(client, ["never-seen"])
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"not_found": 1}
    assert resp.json()["changed"] is False
    assert get(client, "M1").status_code == 404


def test_undo_whole_meter_removes_it_from_queries(client):
    post(client, [reading("M1", "a", 0, 10), reading("M1", "b", 60, 20)])
    undo(client, ["a", "b"])
    assert get(client, "M1").status_code == 404
    assert client.get("/api/v1/meters").json() == []


def test_undo_then_reinsert_same_source(client):
    post(client, [reading("M1", "a", 0, 10), reading("M1", "b", 60, 20)])
    undo(client, ["b"])
    # The source id is free again and accepts new content.
    resp = post(client, [reading("M1", "b", 60, 45)])
    assert resp.status_code == 200
    assert [i["usage"] for i in get(client, "M1").json()["intervals"]] == [35]


def test_undo_is_atomic_across_meters(client):
    post(
        client,
        [
            reading("M1", "a", 0, 10),
            reading("M2", "b", 0, 10),
            reading("M2", "c", 60, 20),
        ],
    )
    resp = undo(client, ["a", "b", "nope"])
    assert resp.status_code == 200
    assert resp.json()["counts"] == {"removed": 2, "not_found": 1}
    # M1 is still affected: it lost its only reading and now has no intervals.
    summaries = resp.json()["affected_meters"]
    assert [m["meter_id"] for m in summaries] == ["M1", "M2"]
    assert summaries[0]["reading_count"] == 0
    assert summaries[0]["interval_count"] == 0
    assert get(client, "M1").status_code == 404
    assert get(client, "M2").json()["readings"][0]["source_record_id"] == "c"


def test_query_window_returns_only_fully_contained_intervals(client):
    post(
        client,
        [reading("M1", chr(ord("a") + i), i * 60, 100 + i * 10) for i in range(4)],
    )
    body = get(client, "M1", **{"from": at(60), "to": at(120)}).json()
    assert [r["source_record_id"] for r in body["readings"]] == ["b", "c"]
    assert len(body["intervals"]) == 1
    assert body["window"]["applied"] is True
    assert_meter_consistent(body)


def test_query_window_validation(client):
    post(client, [reading("M1", "a", 0, 10)])
    assert get(client, "M1", **{"from": "not-a-date"}).status_code == 422
    assert get(client, "M1", **{"from": at(60), "to": at(0)}).status_code == 422


def test_input_validation(client):
    bad = [
        {"meter_id": "", "source_record_id": "x", "read_at": at(0), "reading": 1},
        {"meter_id": "M", "source_record_id": "x", "read_at": at(0), "reading": -1},
        {"meter_id": "M", "source_record_id": "x", "read_at": at(0), "reading": 1.5},
        {"meter_id": "M", "source_record_id": "x", "read_at": "yesterday", "reading": 1},
        {"meter_id": "M", "source_record_id": "x", "read_at": at(0), "reading": 1,
         "extra": True},
    ]
    for payload in bad:
        assert post(client, [payload]).status_code == 422, payload

    assert client.post("/api/v1/readings", json={"readings": []}).status_code == 422
    accepted = post(
        client,
        [
            {
                "meter_id": " M ",
                "source_record_id": " x ",
                "read_at": "2026-01-01T08:00:00+08:00",  # == 00:00Z
                "reading": 7,
                "meter_replaced": 0,
            }
        ],
    )
    assert accepted.status_code == 200
    body = get(client, "M").json()
    assert body["readings"][0]["source_record_id"] == "x"
    assert body["readings"][0]["read_at"] == "2026-01-01T00:00:00Z"

    # The same instant expressed in UTC is a meter-time conflict.
    conflict = post(
        client,
        [
            {
                "meter_id": "M",
                "source_record_id": "y",
                "read_at": "2026-01-01T00:00:00Z",
                "reading": 7,
            }
        ],
    )
    assert conflict.status_code == 409


def test_naive_timestamp_is_treated_as_utc(client):
    resp = post(
        client,
        [
            {
                "meter_id": "M",
                "source_record_id": "a",
                "read_at": "2026-01-01T00:00:00",
                "reading": 1,
            }
        ],
    )
    assert resp.status_code == 200
    assert get(client, "M").json()["readings"][0]["read_at"] == "2026-01-01T00:00:00Z"


def test_include_intervals_false_still_reports_total(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 120),
            reading("M1", "c", 120, 50, replaced=True),
            reading("M1", "d", 180, 80),
        ],
    )
    body = get(client, "M1", include_intervals="false").json()
    assert body["intervals"] == []
    assert body["breaks"] == []
    assert body["usage_total"] == 50  # 20 + 30, break skipped


def test_meter_listing(client):
    post(client, [reading("M2", "a", 0, 10), reading("M2", "b", 60, 30),
                  reading("M1", "c", 0, 9)])
    listing = client.get("/api/v1/meters").json()
    assert [m["meter_id"] for m in listing] == ["M1", "M2"]
    assert listing[1]["usage_total"] == 20


def test_database_matches_query(client, settings):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 150),
        ],
    )
    undo(client, ["c"])
    with connection(settings) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert_db_invariants(conn, "M1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        sequences = conn.execute(
            "SELECT COUNT(*) FROM sqlite_sequence"
        ).fetchone()[0]
        assert sequences >= 0


def test_conflict_is_detected_even_when_other_rows_are_new(client):
    """A rejected batch must not bump the commit sequence."""
    first = post(client, [reading("M1", "a", 0, 10)]).json()
    post(client, [reading("M1", "b", 60, 20), reading("M1", "a", 0, 99)])
    assert get(client, "M1").json()["commit_seq"] == first["commit_seq"]


def test_sqlite_unique_constraints_back_up_application_checks(client, settings):
    post(client, [reading("M1", "a", 0, 10)])
    with connection(settings) as conn:
        existing = conn.execute(
            "SELECT read_at_us FROM readings WHERE source_record_id = 'a'"
        ).fetchone()[0]
    insert = (
        "INSERT INTO readings (source_record_id, meter_id, read_at_us, "
        "reading, meter_replaced, content_hash, created_commit_seq) "
        "VALUES (?, ?, ?, 1, 0, 'h', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        with connection(settings) as conn:
            conn.execute(insert, ("a", "M1", existing + 1))
    with pytest.raises(sqlite3.IntegrityError):
        with connection(settings) as conn:
            conn.execute(insert, ("brand-new", "M1", existing))
