"""Domain service: validate, persist, recompute and query energy intervals.

Invariants
----------
* A batch is applied atomically.  Every conflict is detected during validation,
  before any write, so a rejected batch leaves the database untouched.
* ``(source_record_id)`` is unique.  Re-sending identical content is an
  idempotent no-op; re-sending different content rejects the whole batch.
* ``(meter_id, read_at_us)`` is unique.  Two different source records for the
  same meter and instant reject the whole batch.
* Intervals are always derived from the persisted readings of the commit that
  wrote them.  They are recomputed for every meter touched by a mutation.
* Recomputing a meter means deleting and re-creating *all* of its intervals
  inside the same write transaction.  That makes the interval table a pure
  function of the readings table: after any crash or restart there is no
  possibility of a half-updated interval set.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from .config import Settings
from .correction import PlanningPoint, WindowPlan, plan_window
from .db import (
    bump_commit_seq,
    connection,
    current_commit_seq,
    read_transaction,
    write_transaction,
)
from .models import (
    ConflictDetail,
    CorrectionConfirmResponse,
    CorrectionErrorResponse,
    CorrectionProposalResponse,
    CorrectionSegmentInfo,
    IntervalOut,
    MeterStateResponse,
    MeterSummary,
    MutationResponse,
    QueryWindow,
    ReadingIn,
    ReadingOut,
    RecordResult,
)
from .timeutil import (
    isoformat_utc,
    microseconds_to_iso,
    normalize_utc,
    to_microseconds,
)

BREAK_METER_REPLACED = "meter_replaced"
BREAK_COUNTER_ROLLBACK = "counter_rollback"


class BatchRejected(Exception):
    """The whole request is refused; nothing was written."""

    def __init__(self, message: str, conflicts: Sequence[ConflictDetail]):
        super().__init__(message)
        self.message = message
        self.conflicts = list(conflicts)


class CorrectionRejected(Exception):
    """A correction proposal/confirm cannot be actioned.

    Carries an API error code plus the HTTP status the route should emit.
    Nothing is ever written when this is raised.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def as_response(self) -> CorrectionErrorResponse:
        return CorrectionErrorResponse(
            code=self.code, message=self.message, details=self.details
        )


def _content_hash(reading: ReadingIn) -> str:
    payload = json.dumps(
        {
            "meter_id": reading.meter_id,
            "read_at_us": to_microseconds(reading.read_at),
            "reading": reading.reading,
            "meter_replaced": bool(reading.meter_replaced),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class _Prepared:
    reading: ReadingIn
    read_at_us: int
    content_hash: str


@dataclass
class _Validation:
    to_insert: list[_Prepared] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[ConflictDetail] = field(default_factory=list)


def _existing_by_source(
    conn: sqlite3.Connection, source_ids: Iterable[str]
) -> dict[str, sqlite3.Row]:
    ids = list(dict.fromkeys(source_ids))
    found: dict[str, sqlite3.Row] = {}
    chunk = 400
    for start in range(0, len(ids), chunk):
        part = ids[start : start + chunk]
        placeholders = ",".join("?" for _ in part)
        rows = conn.execute(
            f"SELECT id, source_record_id, meter_id, read_at_us, reading, "
            f"meter_replaced, content_hash FROM readings "
            f"WHERE source_record_id IN ({placeholders})",
            part,
        ).fetchall()
        for row in rows:
            found[row["source_record_id"]] = row
    return found


def _validate_batch(
    conn: sqlite3.Connection, readings: Sequence[ReadingIn]
) -> _Validation:
    result = _Validation()

    # ---- duplicates inside the request itself -------------------------
    by_source: dict[str, _Prepared] = {}
    by_slot: dict[tuple[str, int], _Prepared] = {}
    for reading in readings:
        prepared = _Prepared(
            reading=reading,
            read_at_us=to_microseconds(reading.read_at),
            content_hash=_content_hash(reading),
        )
        earlier = by_source.get(reading.source_record_id)
        if earlier is not None:
            if earlier.content_hash == prepared.content_hash:
                # Exact duplicate in the same batch: collapse to one.
                continue
            result.conflicts.append(
                ConflictDetail(
                    type="source_record_conflict",
                    source_record_id=reading.source_record_id,
                    conflicting_source_record_id=reading.source_record_id,
                    detail=(
                        "同一源记录号在本批内出现多次且内容不同"
                    ),
                )
            )
            continue
        by_source[reading.source_record_id] = prepared

        slot = (reading.meter_id, prepared.read_at_us)
        same_slot = by_slot.get(slot)
        if same_slot is not None:
            # Two distinct source records for one meter at one instant — even
            # with identical content — are an ambiguous input; reject.
            result.conflicts.append(
                ConflictDetail(
                    type="meter_time_conflict",
                    source_record_id=reading.source_record_id,
                    conflicting_source_record_id=same_slot.reading.source_record_id,
                    detail=(
                        f"电表 {reading.meter_id} 在 "
                        f"{isoformat_utc(reading.read_at)} 已有另一条本批读数"
                    ),
                )
            )
            continue
        by_slot[slot] = prepared

    if result.conflicts:
        return result

    # ---- compare with stored readings ---------------------------------
    existing = _existing_by_source(conn, by_source.keys())
    for source_id, prepared in by_source.items():
        stored = existing.get(source_id)
        if stored is not None:
            if (
                stored["content_hash"] == prepared.content_hash
                and stored["meter_id"] == prepared.reading.meter_id
            ):
                result.unchanged.append(source_id)
            else:
                result.conflicts.append(
                    ConflictDetail(
                        type="source_record_conflict",
                        source_record_id=source_id,
                        conflicting_source_record_id=source_id,
                        detail="源记录号已存在但内容不同",
                    )
                )
            continue
        result.to_insert.append(prepared)

    if result.conflicts:
        return result

    # ---- (meter, instant) against stored readings ----------------------
    for prepared in result.to_insert:
        reading = prepared.reading
        row = conn.execute(
            "SELECT source_record_id FROM readings "
            "WHERE meter_id = ? AND read_at_us = ?",
            (reading.meter_id, prepared.read_at_us),
        ).fetchone()
        if row is not None:
            result.conflicts.append(
                ConflictDetail(
                    type="meter_time_conflict",
                    source_record_id=reading.source_record_id,
                    conflicting_source_record_id=row["source_record_id"],
                    detail=(
                        f"电表 {reading.meter_id} 在 "
                        f"{isoformat_utc(reading.read_at)} 已有读数 "
                        f"{row['source_record_id']}"
                    ),
                )
            )
    return result


def _recompute_intervals(
    conn: sqlite3.Connection, meter_id: str, commit_seq: int
) -> None:
    """Rebuild every interval of one meter from its persisted readings."""
    conn.execute("DELETE FROM intervals WHERE meter_id = ?", (meter_id,))
    rows = conn.execute(
        "SELECT id, read_at_us, reading, meter_replaced "
        "FROM readings WHERE meter_id = ? ORDER BY read_at_us ASC",
        (meter_id,),
    ).fetchall()

    payload: list[tuple] = []
    previous: sqlite3.Row | None = None
    for row in rows:
        if previous is None:
            previous = row
            continue
        if bool(row["meter_replaced"]):
            is_break, usage, reason = 1, None, BREAK_METER_REPLACED
        elif row["reading"] < previous["reading"]:
            is_break, usage, reason = 1, None, BREAK_COUNTER_ROLLBACK
        else:
            is_break, usage, reason = 0, row["reading"] - previous["reading"], None
        payload.append(
            (
                meter_id,
                previous["id"],
                row["id"],
                previous["read_at_us"],
                row["read_at_us"],
                previous["reading"],
                row["reading"],
                usage,
                is_break,
                reason,
                commit_seq,
            )
        )
        previous = row

    if payload:
        conn.executemany(
            "INSERT INTO intervals (meter_id, from_reading_id, to_reading_id, "
            "from_at_us, to_at_us, from_value, to_value, usage, is_break, "
            "break_reason, commit_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            payload,
        )


def _meter_summary(conn: sqlite3.Connection, meter_id: str) -> MeterSummary:
    readings = conn.execute(
        "SELECT COUNT(*) AS n, MIN(read_at_us) AS first_at, "
        "MAX(read_at_us) AS last_at FROM readings WHERE meter_id = ?",
        (meter_id,),
    ).fetchone()
    intervals = conn.execute(
        "SELECT COUNT(*) AS n, "
        "COALESCE(SUM(is_break = 0), 0) AS usable, "
        "COALESCE(SUM(is_break = 1), 0) AS breaks, "
        "COALESCE(SUM(usage), 0) AS total "
        "FROM intervals WHERE meter_id = ?",
        (meter_id,),
    ).fetchone()
    reading_count = int(readings["n"])
    return MeterSummary(
        meter_id=meter_id,
        reading_count=reading_count,
        interval_count=int(intervals["n"]),
        usage_interval_count=int(intervals["usable"]),
        break_count=int(intervals["breaks"]),
        usage_total=int(intervals["total"]),
        first_read_at=(
            microseconds_to_iso(readings["first_at"])
            if reading_count
            else None
        ),
        last_read_at=(
            microseconds_to_iso(readings["last_at"])
            if reading_count
            else None
        ),
    )


def _mutation_response(
    conn: sqlite3.Connection,
    commit_seq: int,
    changed: bool,
    records: list[RecordResult],
    meters: Iterable[str],
) -> MutationResponse:
    counts: dict[str, int] = defaultdict(int)
    for record in records:
        counts[record.status] += 1
    summaries = [
        _meter_summary(conn, meter_id) for meter_id in sorted(set(meters))
    ]
    return MutationResponse(
        commit_seq=commit_seq,
        changed=changed,
        counts=dict(counts),
        records=records,
        affected_meters=summaries,
    )


def ingest(
    settings: Settings, readings: Sequence[ReadingIn]
) -> MutationResponse:
    """Atomically insert a batch of readings and refresh affected intervals."""
    with connection(settings) as conn:
        with write_transaction(conn):
            validation = _validate_batch(conn, readings)
            if validation.conflicts:
                raise BatchRejected("整批读数被拒绝", validation.conflicts)

            records = [
                RecordResult(
                    source_record_id=source_id, status="unchanged"
                )
                for source_id in validation.unchanged
            ]
            if not validation.to_insert:
                # Pure idempotent retry: nothing to write, no new commit.
                return _mutation_response(
                    conn,
                    current_commit_seq(conn),
                    changed=False,
                    records=records,
                    meters=[],
                )

            commit_seq = bump_commit_seq(conn)
            meters: set[str] = set()
            for prepared in validation.to_insert:
                reading = prepared.reading
                conn.execute(
                    "INSERT INTO readings (source_record_id, meter_id, "
                    "read_at_us, reading, meter_replaced, content_hash, "
                    "created_commit_seq) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        reading.source_record_id,
                        reading.meter_id,
                        prepared.read_at_us,
                        reading.reading,
                        1 if reading.meter_replaced else 0,
                        prepared.content_hash,
                        commit_seq,
                    ),
                )
                meters.add(reading.meter_id)
                records.append(
                    RecordResult(
                        source_record_id=reading.source_record_id,
                        status="created",
                    )
                )
            for meter_id in sorted(meters):
                _recompute_intervals(conn, meter_id, commit_seq)

            return _mutation_response(
                conn,
                commit_seq,
                changed=True,
                records=records,
                meters=meters,
            )


def undo(
    settings: Settings, source_record_ids: Sequence[str]
) -> MutationResponse:
    """Atomically remove readings by source record id and refresh intervals."""
    ids = list(dict.fromkeys(source_record_ids))
    with connection(settings) as conn:
        with write_transaction(conn):
            existing = _existing_by_source(conn, ids)
            records: list[RecordResult] = []
            meters: set[str] = set()
            to_delete: list[int] = []
            for source_id in ids:
                row = existing.get(source_id)
                if row is None:
                    records.append(
                        RecordResult(
                            source_record_id=source_id, status="not_found"
                        )
                    )
                    continue
                to_delete.append(int(row["id"]))
                meters.add(row["meter_id"])
                records.append(
                    RecordResult(
                        source_record_id=source_id, status="removed"
                    )
                )

            if not to_delete:
                return _mutation_response(
                    conn,
                    current_commit_seq(conn),
                    changed=False,
                    records=records,
                    meters=[],
                )

            commit_seq = bump_commit_seq(conn)
            # intervals reference readings with ON DELETE CASCADE; the affected
            # meters are rebuilt below from the remaining readings.
            conn.executemany(
                "DELETE FROM intervals WHERE from_reading_id = ? "
                "OR to_reading_id = ?",
                [(rid, rid) for rid in to_delete],
            )
            conn.executemany(
                "DELETE FROM readings WHERE id = ?", [(rid,) for rid in to_delete]
            )
            for meter_id in sorted(meters):
                _recompute_intervals(conn, meter_id, commit_seq)

            return _mutation_response(
                conn,
                commit_seq,
                changed=True,
                records=records,
                meters=meters,
            )


def list_meters(settings: Settings) -> list[MeterSummary]:
    """Summarise every meter that currently has at least one reading."""
    with connection(settings) as conn:
        with read_transaction(conn):
            rows = conn.execute(
                "SELECT DISTINCT meter_id FROM readings ORDER BY meter_id"
            ).fetchall()
            return [_meter_summary(conn, row["meter_id"]) for row in rows]


def _parse_window_boundary(raw: str | None, label: str) -> int | None:
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法的 ISO-8601 时刻: {raw!r}") from exc
    return to_microseconds(normalize_utc(parsed))


def _interval_out(row: sqlite3.Row, from_source: str, to_source: str) -> IntervalOut:
    return IntervalOut(
        from_source_record_id=from_source,
        to_source_record_id=to_source,
        from_read_at=microseconds_to_iso(row["from_at_us"]),
        to_read_at=microseconds_to_iso(row["to_at_us"]),
        from_reading=int(row["from_value"]),
        to_reading=int(row["to_value"]),
        usage=None if row["usage"] is None else int(row["usage"]),
        is_break=bool(row["is_break"]),
        break_reason=row["break_reason"],
        commit_seq=int(row["commit_seq"]),
    )


def _reading_out(row: sqlite3.Row) -> ReadingOut:
    return ReadingOut(
        source_record_id=row["source_record_id"],
        read_at=microseconds_to_iso(row["read_at_us"]),
        reading=int(row["reading"]),
        meter_replaced=bool(row["meter_replaced"]),
        commit_seq=int(row["created_commit_seq"]),
    )


def query_meter(
    settings: Settings,
    meter_id: str,
    from_raw: str | None = None,
    to_raw: str | None = None,
    include_intervals: bool = True,
) -> MeterStateResponse | None:
    """Read one consistent snapshot of a meter's readings and intervals."""
    from_us = _parse_window_boundary(from_raw, "from")
    to_us = _parse_window_boundary(to_raw, "to")
    if from_us is not None and to_us is not None and from_us > to_us:
        raise ValueError("from 必须早于或等于 to")
    windowed = from_us is not None or to_us is not None

    with connection(settings) as conn:
        with read_transaction(conn):
            commit_seq = current_commit_seq(conn)
            meter_exists = conn.execute(
                "SELECT 1 FROM readings WHERE meter_id = ? LIMIT 1",
                (meter_id,),
            ).fetchone()
            if meter_exists is None:
                return None

            clauses = ["meter_id = ?"]
            params: list[object] = [meter_id]
            if from_us is not None:
                clauses.append("read_at_us >= ?")
                params.append(from_us)
            if to_us is not None:
                clauses.append("read_at_us <= ?")
                params.append(to_us)
            reading_rows = conn.execute(
                "SELECT source_record_id, read_at_us, reading, meter_replaced, "
                f"created_commit_seq FROM readings WHERE {' AND '.join(clauses)} "
                "ORDER BY read_at_us ASC",
                params,
            ).fetchall()
            readings = [_reading_out(row) for row in reading_rows]

            interval_outs: list[IntervalOut] = []
            if include_intervals:
                # Only intervals fully inside the requested window are returned,
                # so every interval endpoint is also present in `readings`.
                i_clauses = ["i.meter_id = ?"]
                i_params: list[object] = [meter_id]
                if from_us is not None:
                    i_clauses.append("i.from_at_us >= ?")
                    i_params.append(from_us)
                if to_us is not None:
                    i_clauses.append("i.to_at_us <= ?")
                    i_params.append(to_us)
                interval_rows = conn.execute(
                    "SELECT i.from_at_us, i.to_at_us, i.from_value, i.to_value, "
                    "i.usage, i.is_break, i.break_reason, i.commit_seq, "
                    "f.source_record_id AS from_source, "
                    "t.source_record_id AS to_source "
                    "FROM intervals i "
                    "JOIN readings f ON f.id = i.from_reading_id "
                    "JOIN readings t ON t.id = i.to_reading_id "
                    f"WHERE {' AND '.join(i_clauses)} "
                    "ORDER BY i.to_at_us ASC",
                    i_params,
                ).fetchall()
                interval_outs = [
                    _interval_out(row, row["from_source"], row["to_source"])
                    for row in interval_rows
                ]

            breaks = [item for item in interval_outs if item.is_break]
            if include_intervals:
                usage_total = sum(
                    item.usage for item in interval_outs
                    if not item.is_break and item.usage is not None
                )
            else:
                # Intervals were not selected; still report the meter total
                # from the same snapshot.
                total_row = conn.execute(
                    "SELECT COALESCE(SUM(usage), 0) FROM intervals "
                    "WHERE meter_id = ? AND is_break = 0"
                    + (" AND from_at_us >= ?" if from_us is not None else "")
                    + (" AND to_at_us <= ?" if to_us is not None else ""),
                    [
                        value
                        for value in (meter_id, from_us, to_us)
                        if value is not None
                    ],
                ).fetchone()
                usage_total = int(total_row[0])
            return MeterStateResponse(
                meter_id=meter_id,
                commit_seq=commit_seq,
                window=QueryWindow(
                    from_=(
                        microseconds_to_iso(from_us)
                        if from_us is not None
                        else None
                    ),
                    to=(
                        microseconds_to_iso(to_us) if to_us is not None else None
                    ),
                    applied=windowed,
                ),
                readings=readings,
                intervals=interval_outs,
                breaks=breaks,
                usage_total=int(usage_total),
            )


# ---------------------------------------------------------------------------
# Correction proposals and confirmations
# ---------------------------------------------------------------------------


def _load_correction_window(
    conn: sqlite3.Connection,
    meter_id: str,
    start_source_id: str,
    end_source_id: str,
) -> list[sqlite3.Row]:
    """Load the closed time-ordered reading window between two trusted ids."""
    rows = conn.execute(
        "SELECT id, source_record_id, meter_id, read_at_us, reading, "
        "meter_replaced FROM readings WHERE source_record_id IN (?, ?)",
        (start_source_id, end_source_id),
    ).fetchall()
    by_id = {row["source_record_id"]: row for row in rows}
    missing = [
        source_id
        for source_id in (start_source_id, end_source_id)
        if source_id not in by_id
    ]
    if missing:
        raise CorrectionRejected(
            code="correction_endpoint_not_found",
            status_code=404,
            message=f"可信源记录号不存在: {', '.join(missing)}",
            details={source_id: "not_found" for source_id in missing},
        )
    start_row, end_row = by_id[start_source_id], by_id[end_source_id]
    wrong_meter = sorted(
        {
            row["source_record_id"]
            for row in (start_row, end_row)
            if row["meter_id"] != meter_id
        }
    )
    if wrong_meter:
        raise CorrectionRejected(
            code="correction_invalid_range",
            status_code=422,
            message=(
                f"端点记录不属于电表 {meter_id}: {', '.join(wrong_meter)}"
            ),
            details={source_id: "meter_mismatch" for source_id in wrong_meter},
        )
    if start_row["read_at_us"] >= end_row["read_at_us"]:
        raise CorrectionRejected(
            code="correction_invalid_range",
            status_code=422,
            message=(
                "起点源记录号必须在时序上严格早于终点源记录号"
                if start_row["read_at_us"] > end_row["read_at_us"]
                else "两个端点指向同一时刻；同一电表同一时刻只允许一条读数"
            ),
            details={
                "start_source_record_id": start_source_id,
                "end_source_record_id": end_source_id,
            },
        )

    return conn.execute(
        "SELECT id, source_record_id, meter_id, read_at_us, reading, "
        "meter_replaced FROM readings WHERE meter_id = ? "
        "AND read_at_us >= ? AND read_at_us <= ? ORDER BY read_at_us ASC",
        (meter_id, start_row["read_at_us"], end_row["read_at_us"]),
    ).fetchall()


def _plan_from_rows(
    rows: Sequence[sqlite3.Row],
) -> WindowPlan[str]:
    points: list[PlanningPoint[str]] = []
    for index, row in enumerate(rows):
        is_endpoint = index == 0 or index == len(rows) - 1
        points.append(
            PlanningPoint(
                ref=row["source_record_id"],
                value=int(row["reading"]),
                mandatory=is_endpoint or bool(row["meter_replaced"]),
                replaced=bool(row["meter_replaced"]),
            )
        )
    return plan_window(points)


def _segment_infos(plan: WindowPlan[str]) -> list[CorrectionSegmentInfo]:
    infos: list[CorrectionSegmentInfo] = []
    for segment in plan.segments:
        infos.append(
            CorrectionSegmentInfo(
                segment_index=segment.segment_index,
                start_source_record_id=segment.points[0].ref,
                end_source_record_id=segment.points[-1].ref,
                reading_count=len(segment.points),
                mandatory_count=segment.mandatory_count,
                revoke_source_record_ids=list(segment.revoke_refs),
                alternative_revoke_source_record_ids=(
                    list(segment.alternative_revoke_refs)
                    if segment.alternative_revoke_refs is not None
                    else None
                ),
                reason=segment.reason,
            )
        )
    return infos


def correction_propose(
    settings: Settings,
    meter_id: str,
    start_source_id: str,
    end_source_id: str,
) -> CorrectionProposalResponse:
    """Compute the minimum-revocation plan on one read snapshot."""
    with connection(settings) as conn:
        with read_transaction(conn):
            commit_seq = current_commit_seq(conn)
            rows = _load_correction_window(
                conn, meter_id, start_source_id, end_source_id
            )
            plan = _plan_from_rows(rows)

            if not plan.feasible:
                return CorrectionProposalResponse(
                    status="no_solution",
                    reason=plan.reason,
                    meter_id=meter_id,
                    start_source_record_id=start_source_id,
                    end_source_record_id=end_source_id,
                    commit_seq=commit_seq,
                    minimum_revoke_count=None,
                    revoke_source_record_ids=[],
                    segments=_segment_infos(plan),
                )
            if not plan.unique:
                return CorrectionProposalResponse(
                    status="not_unique",
                    reason=plan.reason,
                    meter_id=meter_id,
                    start_source_record_id=start_source_id,
                    end_source_record_id=end_source_id,
                    commit_seq=commit_seq,
                    minimum_revoke_count=None,
                    revoke_source_record_ids=[],
                    segments=_segment_infos(plan),
                )
            return CorrectionProposalResponse(
                status="unique",
                reason=None,
                meter_id=meter_id,
                start_source_record_id=start_source_id,
                end_source_record_id=end_source_id,
                commit_seq=commit_seq,
                minimum_revoke_count=len(plan.revoke_refs),
                revoke_source_record_ids=list(plan.revoke_refs),
                segments=_segment_infos(plan),
            )


def correction_confirm(
    settings: Settings,
    meter_id: str,
    start_source_id: str,
    end_source_id: str,
    expected_commit_seq: int,
    requested_revoke_ids: Sequence[str],
) -> CorrectionConfirmResponse:
    """Re-validate a proposal inside one write transaction and apply it.

    The commit sequence and the unique minimum revoke set are both checked
    against the *current* committed state.  On any mismatch the transaction
    rolls back and neither readings nor intervals change.
    """
    requested_set = set(requested_revoke_ids)
    with connection(settings) as conn:
        with write_transaction(conn):
            current_seq = current_commit_seq(conn)
            if current_seq != expected_commit_seq:
                raise CorrectionRejected(
                    code="correction_stale",
                    status_code=409,
                    message=(
                        f"提案快照序号 {expected_commit_seq} 已过期，"
                        f"当前提交序号为 {current_seq}；请重新提案"
                    ),
                    details={
                        "expected_commit_seq": str(expected_commit_seq),
                        "current_commit_seq": str(current_seq),
                    },
                )

            rows = _load_correction_window(
                conn, meter_id, start_source_id, end_source_id
            )
            plan = _plan_from_rows(rows)
            if not plan.feasible:
                raise CorrectionRejected(
                    code="correction_no_solution",
                    status_code=409,
                    message=plan.reason or "当前状态下不存在可行纠错方案",
                )
            if not plan.unique:
                raise CorrectionRejected(
                    code="correction_not_unique",
                    status_code=409,
                    message=plan.reason or "最少撤销方案不唯一，拒绝自动确认",
                )

            current_plan_set = set(plan.revoke_refs)
            if requested_set != current_plan_set:
                raise CorrectionRejected(
                    code="correction_plan_mismatch",
                    status_code=409,
                    message=(
                        "提交的撤销集合与当前唯一最少方案不一致；请重新提案"
                    ),
                    details={
                        "submitted": ",".join(sorted(requested_set)),
                        "current_plan": ",".join(sorted(current_plan_set)),
                    },
                )

            records = [
                RecordResult(source_record_id=source_id, status="removed")
                for source_id in plan.revoke_refs
            ]
            if not current_plan_set:
                # Nothing to revoke: no mutation, the commit sequence must not
                # advance.
                return CorrectionConfirmResponse(
                    meter_id=meter_id,
                    start_source_record_id=start_source_id,
                    end_source_record_id=end_source_id,
                    commit_seq=current_seq,
                    changed=False,
                    counts={},
                    records=[],
                    affected_meters=[_meter_summary(conn, meter_id)],
                )

            id_rows = conn.execute(
                "SELECT id FROM readings WHERE source_record_id IN (%s)"
                % ",".join("?" for _ in current_plan_set),
                list(current_plan_set),
            ).fetchall()
            reading_ids = [row["id"] for row in id_rows]

            new_commit_seq = bump_commit_seq(conn)
            conn.executemany(
                "DELETE FROM intervals WHERE from_reading_id = ? "
                "OR to_reading_id = ?",
                [(rid, rid) for rid in reading_ids],
            )
            conn.executemany(
                "DELETE FROM readings WHERE id = ?",
                [(rid,) for rid in reading_ids],
            )
            _recompute_intervals(conn, meter_id, new_commit_seq)

            return CorrectionConfirmResponse(
                meter_id=meter_id,
                start_source_record_id=start_source_id,
                end_source_record_id=end_source_id,
                commit_seq=new_commit_seq,
                changed=True,
                counts={"removed": len(reading_ids)},
                records=records,
                affected_meters=[_meter_summary(conn, meter_id)],
            )
