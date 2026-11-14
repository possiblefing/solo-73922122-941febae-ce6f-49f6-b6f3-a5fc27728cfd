"""Pure planning logic for the meter-reading correction feature.

Problem
-------
Given one meter's readings inside an engineer-supplied, time-ordered closed
range ``[start, end]``, find the *minimum* number of readings to revoke so
that every reading kept is non-decreasing **within each meter-replacement
segment**:

* the two range endpoints are trusted and must be kept;
* every reading flagged ``meter_replaced`` starts a new segment and must be
  kept (so does a replacement-flagged endpoint);
* non-decreasing constraints never cross a replacement boundary;
* if no feasible kept set exists (a mandatory reading cannot be reached from
  the previous mandatory reading without decreasing) there is *no solution*;
* if several distinct revoke sets share the minimum size the plan is *not
  unique* and the API must not pick one.

Algorithm
---------
Replacement flags split the window into independent segments, each starting
on a mandatory reading and ending just before the next replacement.  Inside
one segment the mandatory readings (its start, plus the window end for the
final segment) form a fixed skeleton.  A single ``O(n^2)`` anchored
longest-non-decreasing-subsequence dynamic program runs over the segment:

* every chain starts at the segment's first (mandatory) point;
* a transition may not jump over a mandatory point, so every reachable chain
  visits the mandatory readings in order;
* the optimum is the longest chain ending at the last mandatory point, plus
  the best extension through the optional readings after it.

Uniqueness is decided by actually constructing **two** distinct optimal
chains from the DP table (a lowest-index backtrack and a highest-index
backtrack).  If both constructions coincide the optimum is unique; otherwise
they are two concrete, equally optimal kept sets — and hence two minimum
revoke sets.  The module is free of SQLite/FastAPI imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class PlanningPoint(Generic[T]):
    """One reading participating in a plan.

    ``ref`` is an opaque reference (e.g. a source record id); ``mandatory``
    marks endpoints and meter-replacement readings that may not be revoked.
    """

    ref: T
    value: int
    mandatory: bool
    replaced: bool


@dataclass
class SegmentPlan(Generic[T]):
    segment_index: int
    points: list[PlanningPoint[T]]
    mandatory_count: int = 0
    feasible: bool = True
    unique: bool = True
    revoke_refs: list[T] = field(default_factory=list)
    alternative_revoke_refs: list[T] | None = None
    reason: str | None = None


@dataclass
class WindowPlan(Generic[T]):
    feasible: bool
    unique: bool
    revoke_refs: list[T]
    segments: list[SegmentPlan[T]]
    reason: str | None = None
    alternative_revoke_refs: list[T] | None = None

    @property
    def minimum_revoke_count(self) -> int | None:
        return len(self.revoke_refs) if self.feasible and self.unique else None


@dataclass
class _DpTable:
    length: list[int]
    ways: list[int]


def _run_dp(points: list[PlanningPoint[T]]) -> _DpTable:
    """Anchored LNDS dynamic program over one segment's points.

    Every counted chain starts at index 0 (the mandatory segment start).  A
    transition ``j -> i`` is allowed only when:

    * ``points[j].value <= points[i].value`` (non-decreasing), and
    * no mandatory point lies strictly between ``j`` and ``i`` (mandatory
      readings may never be skipped).

    ``ways`` is capped at two; reachability and uniqueness only need
    zero / one / many.  Optimal chains are rebuilt from ``length`` and
    ``ways`` directly, so no predecessor pointers are stored.
    """
    n = len(points)
    length = [1] * n
    ways = [0] * n
    ways[0] = 1

    for i in range(1, n):
        for j in range(i):
            if ways[j] == 0 or points[j].value > points[i].value:
                continue
            if any(points[k].mandatory for k in range(j + 1, i)):
                continue
            candidate = length[j] + 1
            if candidate > length[i]:
                length[i] = candidate
                ways[i] = ways[j]
            elif candidate == length[i]:
                ways[i] = min(2, ways[i] + ways[j])
    return _DpTable(length=length, ways=ways)


def _backtrack(
    points: list[PlanningPoint[T]],
    dp: _DpTable,
    end: int,
    *,
    highest: bool,
) -> list[int] | None:
    """Rebuild one optimum-length chain ending at ``end``.

    ``highest=False`` picks the lowest-indexed feasible predecessor at every
    level (the canonical chain); ``highest=True`` picks the highest-indexed
    one.  Both obey the same non-decreasing and no-mandatory-skip rules, so a
    difference between the two reconstructions certifies two distinct optima.
    """
    chain = [end]
    target = dp.length[end] - 1
    cursor = end
    while target > 0:
        candidates = [
            j
            for j in range(cursor)
            if dp.length[j] == target
            and dp.ways[j] > 0
            and points[j].value <= points[cursor].value
            and not any(points[k].mandatory for k in range(j + 1, cursor))
        ]
        if not candidates:
            return None
        cursor = candidates[-1] if highest else candidates[0]
        chain.append(cursor)
        target -= 1
    chain.reverse()
    if chain[0] != 0:
        return None
    return chain


def _solve_segment(
    points: list[PlanningPoint[T]],
) -> tuple[bool, bool, list[int] | None, list[int] | None, str | None]:
    """Return ``(feasible, unique, primary_kept, alternative_kept, reason)``.

    Kept indices are local to ``points``.  The alternative is ``None`` when
    the optimum is unique.
    """
    n = len(points)
    mandatory_indices = [i for i, p in enumerate(points) if p.mandatory]
    dp = _run_dp(points)

    # Every mandatory point must be reachable from the anchored start.
    unreachable = [i for i in mandatory_indices if dp.ways[i] == 0]
    if unreachable:
        bad = unreachable[0]
        previous = max((i for i in mandatory_indices if i < bad), default=0)
        reason = (
            f"必留读数 {points[previous].ref} 的值 {points[previous].value} "
            f"无法在非递减条件下连接到必留读数 {points[bad].ref} 的值 "
            f"{points[bad].value}"
        )
        return False, False, None, None, reason

    last_mandatory = mandatory_indices[-1]

    # The optimum must contain every mandatory point.  Its chain ends at the
    # last mandatory point, possibly extended by optional points after it;
    # pick the globally longest reachable chain that covers the mandatory set.
    eligible_ends = [last_mandatory] + [
        i for i in range(last_mandatory + 1, n) if dp.ways[i] > 0
    ]
    best_length = max(dp.length[i] for i in eligible_ends)
    best_ends = [i for i in eligible_ends if dp.length[i] == best_length]
    end = best_ends[0]

    primary = _backtrack(points, dp, end, highest=False)
    if primary is None or any(
        i not in set(primary) for i in mandatory_indices
    ):  # pragma: no cover - DP invariants guarantee this
        return False, False, None, None, "必留读数无法同时落在一个非递减子序列中"

    # A distinct optimum either ends at another best-length index, or is a
    # different same-length chain ending at the same index.
    alternative: list[int] | None = None
    for candidate_end in best_ends:
        candidate = _backtrack(
            points, dp, candidate_end, highest=True
        )
        if candidate is not None and candidate != primary:
            alternative = candidate
            break
    if alternative is None:
        # Highest-index backtrack can coincide with the canonical one even
        # when a middle fork exists; try every end with the opposite choice.
        for candidate_end in reversed(best_ends):
            candidate = _backtrack(
                points, dp, candidate_end, highest=False
            )
            if candidate is not None and candidate != primary:
                alternative = candidate
                break

    if alternative is None:
        return True, True, primary, None, None
    return True, False, primary, alternative, None


def plan_window(points: list[PlanningPoint[T]]) -> WindowPlan[T]:
    """Split one time-ordered window at replacement flags and solve it."""
    if not points:
        raise ValueError("plan_window requires at least one point")
    if not points[0].mandatory or not points[-1].mandatory:
        raise ValueError("window endpoints must be mandatory")

    # Replacement-flagged readings start a new segment.
    cuts = [0]
    for i in range(1, len(points)):
        if points[i].replaced:
            cuts.append(i)
    cuts.append(len(points))

    segments: list[SegmentPlan[T]] = []
    canonical_revoke: list[T] = []
    alternative_revoke: list[T] = []
    any_infeasible = False
    any_ambiguous = False
    infeasible_reason: str | None = None

    for seg_index, (lo, hi) in enumerate(zip(cuts, cuts[1:])):
        seg_points = points[lo:hi]
        segment = SegmentPlan(
            segment_index=seg_index,
            points=seg_points,
            mandatory_count=sum(1 for p in seg_points if p.mandatory),
        )
        feasible, unique, primary, alternative, reason = _solve_segment(
            seg_points
        )

        if not feasible:
            segment.feasible = False
            segment.unique = False
            segment.reason = reason
            any_infeasible = True
            infeasible_reason = reason
            segments.append(segment)
            continue

        assert primary is not None
        primary_set = set(primary)
        seg_revoke = [
            point.ref
            for local, point in enumerate(seg_points)
            if local not in primary_set
        ]
        segment.revoke_refs = seg_revoke
        canonical_revoke.extend(seg_revoke)

        if not unique:
            segment.unique = False
            assert alternative is not None
            alt_set = set(alternative)
            segment.alternative_revoke_refs = [
                point.ref
                for local, point in enumerate(seg_points)
                if local not in alt_set
            ]
            segment.reason = (
                f"段 {seg_index} 存在 {len(seg_revoke)} 条撤销的多个并列最少方案"
            )
            any_ambiguous = True
            alternative_revoke.extend(segment.alternative_revoke_refs)
        else:
            alternative_revoke.extend(seg_revoke)
        segments.append(segment)

    if any_infeasible:
        return WindowPlan(
            feasible=False,
            unique=False,
            revoke_refs=[],
            segments=segments,
            reason=infeasible_reason,
        )
    if any_ambiguous:
        return WindowPlan(
            feasible=True,
            unique=False,
            revoke_refs=[],
            segments=segments,
            reason=next(
                (s.reason for s in segments if not s.unique), "最少方案不唯一"
            ),
            alternative_revoke_refs=alternative_revoke,
        )
    return WindowPlan(
        feasible=True,
        unique=True,
        revoke_refs=canonical_revoke,
        segments=segments,
    )
