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


class CorrectionProposeRequest(BaseModel):
    """纠错提案：同一电表时序上两个可信源记录号划定的闭区间。"""

    model_config = ConfigDict(extra="forbid")

    meter_id: str = Field(..., min_length=1, max_length=128, description="电表号")
    start_source_record_id: str = Field(
        ..., min_length=1, max_length=255, description="时序在先的可信源记录号（含）"
    )
    end_source_record_id: str = Field(
        ..., min_length=1, max_length=255, description="时序在后的可信源记录号（含）"
    )

    @field_validator("meter_id", "start_source_record_id", "end_source_record_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class CorrectionConfirmRequest(CorrectionProposeRequest):
    """工程师确认：携带提案快照序号与提案给出的待撤销集合。"""

    model_config = ConfigDict(extra="forbid")

    expected_commit_seq: int = Field(
        ..., ge=0, description="提案返回的已提交快照提交序号"
    )
    revoke_source_record_ids: list[str] = Field(
        ...,
        min_length=0,
        max_length=1000,
        description="提案给出的待撤销源记录号（允许为空集，表示无需撤销）",
    )

    @field_validator("revoke_source_record_ids")
    @classmethod
    def _revoke_not_blank(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                raise ValueError(
                    "revoke_source_record_ids must not contain blank values"
                )
            cleaned.append(value)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("revoke_source_record_ids must not contain duplicates")
        return cleaned


class CorrectionSegmentInfo(BaseModel):
    segment_index: int = Field(..., description="换表边界切出的段序号，从 0 开始")
    start_source_record_id: str
    end_source_record_id: str
    reading_count: int = Field(..., description="段内读数条数（含两端/换表点）")
    mandatory_count: int = Field(..., description="必须保留的读数条数")
    revoke_source_record_ids: list[str] = Field(
        default_factory=list, description="该段最少撤销方案的源记录号；无解时为空"
    )
    alternative_revoke_source_record_ids: list[str] | None = Field(
        default=None, description="方案不唯一时，另一个同等长度的最少撤销集合"
    )
    reason: str | None = Field(
        default=None, description="该段无解/不唯一的具体原因"
    )


class CorrectionProposalResponse(BaseModel):
    status: Literal["unique", "no_solution", "not_unique"]
    reason: str | None = Field(
        default=None, description="status 非 unique 时的原因说明"
    )
    meter_id: str
    start_source_record_id: str
    end_source_record_id: str
    commit_seq: int = Field(..., description="提案所依据的已提交快照提交序号")
    minimum_revoke_count: int | None = Field(
        default=None, description="最少需撤销读数条数；无解时为 null"
    )
    revoke_source_record_ids: list[str] = Field(
        default_factory=list,
        description="唯一最少方案的待撤销源记录号（时序顺序）；非 unique 时为空",
    )
    segments: list[CorrectionSegmentInfo] = Field(
        default_factory=list, description="各换表段的求解明细"
    )


class CorrectionConfirmResponse(BaseModel):
    meter_id: str
    start_source_record_id: str
    end_source_record_id: str
    commit_seq: int = Field(..., description="确认生效后的最新提交序号")
    changed: bool
    counts: dict[str, int]
    records: list[RecordResult]
    affected_meters: list[MeterSummary]


class CorrectionErrorResponse(BaseModel):
    code: Literal[
        "correction_no_solution",
        "correction_not_unique",
        "correction_stale",
        "correction_plan_mismatch",
        "correction_invalid_range",
        "correction_endpoint_not_found",
    ]
    message: str
    details: dict[str, str] | None = None


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
