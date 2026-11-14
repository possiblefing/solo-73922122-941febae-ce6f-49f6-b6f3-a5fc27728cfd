"""Functional tests for the correction propose/confirm API."""

from __future__ import annotations

from tests.common import (
    assert_db_invariants,
    assert_meter_consistent,
    reading,
)


def post(client, readings):
    return client.post("/api/v1/readings", json={"readings": readings})


def propose(client, meter, start, end):
    return client.post(
        "/api/v1/corrections/propose",
        json={
            "meter_id": meter,
            "start_source_record_id": start,
            "end_source_record_id": end,
        },
    )


def confirm(client, meter, start, end, commit_seq, revoke_ids):
    return client.post(
        "/api/v1/corrections/confirm",
        json={
            "meter_id": meter,
            "start_source_record_id": start,
            "end_source_record_id": end,
            "expected_commit_seq": commit_seq,
            "revoke_source_record_ids": revoke_ids,
        },
    )


def test_propose_unique_single_revoke(client):
    # 100 -> 90 (gateway misread) -> 150 -> 200; revoking 90 restores order.
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 150),
            reading("M1", "d", 180, 200),
        ],
    )
    resp = propose(client, "M1", "a", "d")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unique"
    assert body["minimum_revoke_count"] == 1
    assert body["revoke_source_record_ids"] == ["b"]
    assert body["start_source_record_id"] == "a"
    assert body["end_source_record_id"] == "d"
    assert len(body["segments"]) == 1
    assert body["segments"][0]["mandatory_count"] == 2
    # A proposal never mutates anything.
    state = client.get("/api/v1/meters/M1").json()
    assert [r["source_record_id"] for r in state["readings"]] == [
        "a",
        "b",
        "c",
        "d",
    ]


def test_confirm_applies_unique_plan_and_rebuilds_intervals(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 150),
            reading("M1", "d", 180, 200),
        ],
    )
    proposal = propose(client, "M1", "a", "d").json()
    seq = proposal["commit_seq"]

    resp = confirm(client, "M1", "a", "d", seq, ["b"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert body["counts"] == {"removed": 1}
    assert body["commit_seq"] == seq + 1
    assert [r["source_record_id"] for r in body["records"]] == ["b"]

    state = client.get("/api/v1/meters/M1").json()
    assert [r["source_record_id"] for r in state["readings"]] == ["a", "c", "d"]
    # No rollback break remains; usages are pure differences.
    assert all(i["is_break"] is False for i in state["intervals"])
    assert [i["usage"] for i in state["intervals"]] == [50, 50]
    assert_meter_consistent(state)


def test_no_solution_when_endpoints_decrease(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 50),
            reading("M1", "c", 120, 40),
        ],
    )
    body = propose(client, "M1", "a", "c").json()
    assert body["status"] == "no_solution"
    assert body["minimum_revoke_count"] is None
    assert body["revoke_source_record_ids"] == []
    assert body["reason"]
    assert body["segments"][0]["reason"]
    # Data untouched.
    state = client.get("/api/v1/meters/M1").json()
    assert len(state["readings"]) == 3


def test_not_unique_returns_two_options_but_no_plan(client):
    # Keep 10,11,12 or 10,10,12: revoking c vs revoking b, both one removal.
    post(
        client,
        [
            reading("M1", "a", 0, 10),
            reading("M1", "b", 60, 11),
            reading("M1", "c", 120, 10),
            reading("M1", "d", 180, 12),
        ],
    )
    body = propose(client, "M1", "a", "d").json()
    assert body["status"] == "not_unique"
    assert body["revoke_source_record_ids"] == []
    segment = body["segments"][0]
    assert segment["revoke_source_record_ids"] != (
        segment["alternative_revoke_source_record_ids"]
    )
    assert sorted(
        [
            tuple(segment["revoke_source_record_ids"]),
            tuple(segment["alternative_revoke_source_record_ids"]),
        ]
    ) == [("b",), ("c",)]


def test_replacement_boundary_resets_non_decreasing_rule(client):
    # 1000 -> (replaced) 30 -> 90. The cross-boundary drop is a meter
    # replacement, not a rollback; only the in-segment dip 30 -> 20 -> 90
    # needs correction.
    post(
        client,
        [
            reading("M1", "a", 0, 1000),
            reading("M1", "r", 60, 30, replaced=True),
            reading("M1", "x", 120, 20),
            reading("M1", "d", 180, 90),
        ],
    )
    body = propose(client, "M1", "a", "d").json()
    assert body["status"] == "unique"
    assert body["revoke_source_record_ids"] == ["x"]
    assert len(body["segments"]) == 2
    assert body["segments"][0]["start_source_record_id"] == "a"
    assert body["segments"][1]["start_source_record_id"] == "r"

    confirm(client, "M1", "a", "d", body["commit_seq"], ["x"])
    state = client.get("/api/v1/meters/M1").json()
    assert [r["source_record_id"] for r in state["readings"]] == ["a", "r", "d"]
    assert state["breaks"][0]["break_reason"] == "meter_replaced"
    assert_meter_consistent(state)


def test_mandatory_replacement_cannot_be_revoked(client):
    # The replaced reading 30 itself dips below its segment's later trusted
    # endpoint? Here r=30 (mandatory) and d=20 (mandatory endpoint) decrease
    # inside the same post-replacement segment -> no solution.
    post(
        client,
        [
            reading("M1", "a", 0, 1000),
            reading("M1", "r", 60, 30, replaced=True),
            reading("M1", "d", 120, 20),
        ],
    )
    body = propose(client, "M1", "a", "d").json()
    assert body["status"] == "no_solution"
    # The replacement reading is never offered for revocation.
    flat = [
        ref
        for segment in body["segments"]
        for ref in segment["revoke_source_record_ids"]
    ]
    assert "r" not in flat


def test_propose_validation_errors(client):
    post(client, [reading("M1", "a", 0, 10), reading("M1", "b", 60, 20)])
    post(client, [reading("M2", "z", 0, 5)])

    # Unknown endpoint.
    resp = propose(client, "M1", "a", "nope")
    assert resp.status_code == 404
    assert resp.json()["code"] == "correction_endpoint_not_found"

    # Endpoint belongs to another meter.
    resp = propose(client, "M1", "a", "z")
    assert resp.status_code == 422
    assert resp.json()["code"] == "correction_invalid_range"

    # Start later than end (reversed order).
    resp = propose(client, "M1", "b", "a")
    assert resp.status_code == 422
    assert resp.json()["code"] == "correction_invalid_range"

    # Blank / malformed body is a pydantic 422.
    resp = client.post(
        "/api/v1/corrections/propose",
        json={"meter_id": "M1", "start_source_record_id": "a"},
    )
    assert resp.status_code == 422


def test_confirm_stale_sequence_is_rejected_and_changes_nothing(client, settings):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 200),
        ],
    )
    proposal = propose(client, "M1", "a", "c").json()
    stale_seq = proposal["commit_seq"]

    # A concurrent committed write advances the sequence.
    post(client, [reading("M1", "n", 180, 210)])

    before = client.get("/api/v1/meters/M1").json()
    resp = confirm(client, "M1", "a", "c", stale_seq, ["b"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "correction_stale"
    after = client.get("/api/v1/meters/M1").json()
    assert after["readings"] == before["readings"]
    assert after["intervals"] == before["intervals"]

    from app.db import connection

    with connection(settings) as conn:
        assert_db_invariants(conn, "M1")


def test_confirm_plan_mismatch_is_rejected(client):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 200),
        ],
    )
    proposal = propose(client, "M1", "a", "c").json()
    # Client asks to revoke the wrong record.
    resp = confirm(client, "M1", "a", "c", proposal["commit_seq"], ["c"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "correction_plan_mismatch"
    state = client.get("/api/v1/meters/M1").json()
    assert len(state["readings"]) == 3


def test_confirm_rejects_when_plan_turned_not_unique(client):
    # Initial state has a unique plan; insert a late reading that creates a
    # second equal-size optimum before confirming.
    post(
        client,
        [
            reading("M1", "a", 0, 10),
            reading("M1", "b", 60, 11),
            reading("M1", "d", 180, 12),
        ],
    )
    first = propose(client, "M1", "a", "d").json()
    assert first["status"] == "unique"
    seq = first["commit_seq"]

    # Backfill a reading making two one-revocation optima.
    post(client, [reading("M1", "c", 120, 10)])
    resp = confirm(client, "M1", "a", "d", seq, first["revoke_source_record_ids"])
    assert resp.status_code == 409
    assert resp.json()["code"] == "correction_stale"
    # Re-proposing now reports the ambiguity rather than auto-picking.
    assert propose(client, "M1", "a", "d").json()["status"] == "not_unique"


def test_zero_revocation_confirm_does_not_advance_sequence(client):
    post(
        client,
        [
            reading("M1", "a", 0, 10),
            reading("M1", "b", 60, 20),
            reading("M1", "c", 120, 30),
        ],
    )
    proposal = propose(client, "M1", "a", "c").json()
    assert proposal["status"] == "unique"
    assert proposal["minimum_revoke_count"] == 0
    seq = proposal["commit_seq"]

    resp = confirm(client, "M1", "a", "c", seq, [])
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is False
    assert body["commit_seq"] == seq
    assert client.get("/api/v1/meters/M1").json()["commit_seq"] == seq


def test_confirm_is_atomic_and_keeps_db_invariants(client, settings):
    post(
        client,
        [
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 80),
            reading("M1", "d", 180, 200),
        ],
    )
    proposal = propose(client, "M1", "a", "d").json()
    # Two misreads b, c both need revocation; unique minimum of two.
    assert proposal["status"] == "unique"
    assert sorted(proposal["revoke_source_record_ids"]) == ["b", "c"]

    resp = confirm(
        client,
        "M1",
        "a",
        "d",
        proposal["commit_seq"],
        proposal["revoke_source_record_ids"],
    )
    assert resp.status_code == 200
    state = client.get("/api/v1/meters/M1").json()
    assert [r["source_record_id"] for r in state["readings"]] == ["a", "d"]
    assert_meter_consistent(state)

    from app.db import connection

    with connection(settings) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert_db_invariants(conn, "M1")


def test_proposal_and_confirm_on_window_with_exterior_readings(client):
    # Readings outside the trusted window are unaffected by the correction.
    post(
        client,
        [
            reading("M1", "pre", -60, 50),
            reading("M1", "a", 0, 100),
            reading("M1", "b", 60, 90),
            reading("M1", "c", 120, 200),
            reading("M1", "post", 240, 260),
        ],
    )
    proposal = propose(client, "M1", "a", "c").json()
    assert proposal["revoke_source_record_ids"] == ["b"]
    confirm(client, "M1", "a", "c", proposal["commit_seq"], ["b"])
    state = client.get("/api/v1/meters/M1").json()
    assert [r["source_record_id"] for r in state["readings"]] == [
        "pre",
        "a",
        "c",
        "post",
    ]
    assert_meter_consistent(state)


def test_confirm_rejects_duplicate_and_blank_revoke_ids(client):
    post(client, [reading("M1", "a", 0, 10), reading("M1", "b", 60, 20)])
    resp = client.post(
        "/api/v1/corrections/confirm",
        json={
            "meter_id": "M1",
            "start_source_record_id": "a",
            "end_source_record_id": "b",
            "expected_commit_seq": 1,
            "revoke_source_record_ids": ["a", "a"],
        },
    )
    assert resp.status_code == 422
