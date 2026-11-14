"""Request / response schemas for the energy reading API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReadingIn(BaseModel):
    """One cumulative meter reading supplied by a gateway."""

    model_config = ConfigDict(extra="forbid")

    meter_id: str = Field(
        ..., min_length=1, max_length=128, description="电表号"
    )
    source_record_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="源记录号；同一号码只允许对应同一条读数",
    )
    read_at: datetime = Field(
        ...,
        description="UTC 时刻，ISO-8601；带时区的值会换算为 UTC，不带时区按 UTC 解释",
    )
    reading: int = Field(
        ..., ge=0, description="电表累计读数（非负整数，单位：度）"
    )
    meter_replaced: bool = Field(
        False, description="该读数起启用新电表；为 true 时与上一读数之间记断点"
    )

    @field_validator("meter_id", "source_record_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("reading", mode="before")
    @classmethod
    def _integral_reading(cls, value: object) -> object:
        # Accept integer literals, integral floats and digit strings; reject
        # booleans, fractions and negative values.
        if isinstance(value, bool):
            raise ValueError("reading must be an integer, not a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError("reading must be an integer")
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
            raise ValueError("reading must be a non-negative integer")
        return value

    @field_validator("meter_replaced", mode="before")
    @classmethod
    def _flag(cls, value: object) -> object:
        # Accept true/false and the common gateway encodings 1/0.
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError("meter_replaced must be true, false, 1 or 0")


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    readings: list[ReadingIn] = Field(
        ..., min_length=1, max_length=1000, description="一批读数，整批原子入仓"
    )


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="按源记录号撤销；不存在的号码按“无变化”处理",
    )

    @field_validator("source_record_ids")
    @classmethod
    def _ids_not_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError("source_record_ids must not contain blank values")
            cleaned.append(value)
        return cleaned


class RecordResult(BaseModel):
    source_record_id: str
    status: Literal["created", "unchanged", "removed", "not_found"]


class MeterSummary(BaseModel):
    meter_id: str
    reading_count: int
    interval_count: int
    usage_interval_count: int
    break_count: int
    usage_total: int
    first_read_at: str | None
    last_read_at: str | None


class MutationResponse(BaseModel):
    commit_seq: int
    changed: bool
    counts: dict[str, int]
    records: list[RecordResult]
    affected_meters: list[MeterSummary]


class ReadingOut(BaseModel):
    source_record_id: str
    read_at: str
    reading: int
    meter_replaced: bool
    commit_seq: int


class IntervalOut(BaseModel):
    from_source_record_id: str
    to_source_record_id: str
    from_read_at: str
    to_read_at: str
    from_reading: int
    to_reading: int
    usage: int | None = Field(
        description="区间用电量；断点区间为 null（不推算该区间用量）"
    )
    is_break: bool
    break_reason: Literal["meter_replaced", "counter_rollback"] | None
    commit_seq: int


class QueryWindow(BaseModel):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    applied: bool


class MeterStateResponse(BaseModel):
    meter_id: str
    commit_seq: int
    window: QueryWindow
    readings: list[ReadingOut]
    intervals: list[IntervalOut]
    breaks: list[IntervalOut]
    usage_total: int


class HealthResponse(BaseModel):
    status: str
    commit_seq: int
    schema_version: int


class ConflictDetail(BaseModel):
    type: str
    source_record_id: str | None
    conflicting_source_record_id: str | None
    detail: str


class ConflictResponse(BaseModel):
    code: Literal["batch_rejected"]
    message: str
    conflicts: list[ConflictDetail]


class ErrorResponse(BaseModel):
    code: str
    message: str
