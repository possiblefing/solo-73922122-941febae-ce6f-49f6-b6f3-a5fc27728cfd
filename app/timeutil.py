"""UTC timestamp helpers.

Timestamps are stored as integer microseconds since the Unix epoch.  Integers
avoid any dependence on textual formatting, timezone offsets or SQLite's
date parsing when comparing readings and enforcing the
``(meter_id, timestamp)`` uniqueness rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def normalize_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime.

    A timestamp without a timezone is interpreted as UTC, which is convenient
    for gateways that omit the suffix.  Explicit offsets are converted to UTC.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_microseconds(value: datetime) -> int:
    value = normalize_utc(value)
    delta = value - _EPOCH
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def isoformat_utc(value: datetime) -> str:
    """Render a UTC datetime as ISO-8601 with a trailing ``Z``."""
    utc_value = normalize_utc(value)
    return utc_value.isoformat().replace("+00:00", "Z")


def microseconds_to_iso(value: int) -> str:
    return isoformat_utc(from_microseconds(value))
