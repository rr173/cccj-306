"""Request schemas. Responses are assembled as plain JSON-compatible dicts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"


def normalize_category(value: str) -> str:
    value = value.strip().upper()
    if not value:
        raise ValueError("course category cannot be empty")
    return value


class SchoolIn(BaseModel):
    school_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)


class StudentIn(BaseModel):
    student_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)


class RecordIn(BaseModel):
    record_code: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    source_school_id: str = Field(min_length=1, max_length=64)
    student_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=32)
    points: int = Field(ge=0, le=1_000_000)

    @field_validator("category")
    @classmethod
    def _category_upper(cls, value: str) -> str:
        return normalize_category(value)


class RevocationIn(BaseModel):
    source_school_id: str = Field(min_length=1, max_length=64)
    reason: Optional[str] = Field(default=None, max_length=500)


class RequirementIn(BaseModel):
    requirement_code: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    name: Optional[str] = Field(default=None, max_length=200)
    accepted_categories: list[str] = Field(min_length=1)
    min_points: int = Field(ge=0, le=1_000_000)
    points_mode: Literal["whole_record", "sum_points"] = "whole_record"

    @field_validator("accepted_categories")
    @classmethod
    def _categories_normalized(cls, values: list[str]) -> list[str]:
        normalized = [normalize_category(v) for v in values]
        if len(set(normalized)) != len(normalized):
            raise ValueError("accepted_categories must be distinct")
        return normalized


class DefinitionIn(BaseModel):
    definition_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    issuer_school_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    requirements: list[RequirementIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _requirement_codes_unique(self) -> "DefinitionIn":
        codes = [r.requirement_code for r in self.requirements]
        if len(set(codes)) != len(codes):
            raise ValueError("requirement_code values must be distinct within a credential")
        return self


class ReviewIn(BaseModel):
    definition_id: str = Field(min_length=1, max_length=64)
    student_id: str = Field(min_length=1, max_length=64)
    record_codes: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _record_codes_unique(self) -> "ReviewIn":
        if len(set(self.record_codes)) != len(self.record_codes):
            raise ValueError("record_codes must not contain duplicates")
        return self


class AssignmentIn(BaseModel):
    requirement_code: str = Field(min_length=1, max_length=64)
    record_code: str = Field(min_length=1, max_length=64)


class IssueIn(BaseModel):
    definition_id: str = Field(min_length=1, max_length=64)
    student_id: str = Field(min_length=1, max_length=64)
    record_codes: list[str] = Field(min_length=1)
    assignments: Optional[list[AssignmentIn]] = None
    certificate_code: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=ID_PATTERN
    )

    @model_validator(mode="after")
    def _validate_payload(self) -> "IssueIn":
        if len(set(self.record_codes)) != len(self.record_codes):
            raise ValueError("record_codes must not contain duplicates")
        if self.assignments is not None:
            rec_codes = [a.record_code for a in self.assignments]
            if len(set(rec_codes)) != len(rec_codes):
                raise ValueError("assignments must not split or repeat a record")
            pairs = [(a.requirement_code, a.record_code) for a in self.assignments]
            if len(set(pairs)) != len(pairs):
                raise ValueError("assignments contain a duplicate requirement/record pair")
        return self


class ConfirmRevocationIn(BaseModel):
    issuer_school_id: str = Field(min_length=1, max_length=64)


# ---------------------------------------------------------------------------
# Advanced practical training: terms, classes, enrollments and joint swaps
# ---------------------------------------------------------------------------


class TrainingTermIn(BaseModel):
    term_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=ID_PATTERN
    )
    organizer_school_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    accepted_definition_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _accepted_unique(self) -> "TrainingTermIn":
        if len(set(self.accepted_definition_ids)) != len(self.accepted_definition_ids):
            raise ValueError("accepted_definition_ids must not contain duplicates")
        return self


class TrainingClassIn(BaseModel):
    class_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=ID_PATTERN
    )
    term_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=0, le=1_000_000)


class EnrollmentIn(BaseModel):
    term_id: str = Field(min_length=1, max_length=64)
    student_id: str = Field(min_length=1, max_length=64)
    class_id: str = Field(min_length=1, max_length=64)
    certificate_code: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)


class WithdrawalIn(BaseModel):
    term_id: str = Field(min_length=1, max_length=64)
    student_id: str = Field(min_length=1, max_length=64)


class SwapParticipantIn(BaseModel):
    student_id: str = Field(min_length=1, max_length=64)
    to_class_id: str = Field(min_length=1, max_length=64)


class SwapOrderIn(BaseModel):
    order_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=ID_PATTERN
    )
    term_id: str = Field(min_length=1, max_length=64)
    organizer_school_id: str = Field(min_length=1, max_length=64)
    confirm_deadline: datetime
    participants: list[SwapParticipantIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _participants_unique(self) -> "SwapOrderIn":
        student_ids = [p.student_id for p in self.participants]
        if len(set(student_ids)) != len(student_ids):
            raise ValueError("each student may appear at most once in a swap order")
        return self


class SwapResponseIn(BaseModel):
    student_id: str = Field(min_length=1, max_length=64)
    response: Literal["confirmed", "rejected"]


# ---------------------------------------------------------------------------
# Shared equipment room: physical items and class reservations
# ---------------------------------------------------------------------------

class EquipmentItemIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    owner_school_id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=200)
    tags: list[str] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _tags_normalized(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            tag = value.strip()
            if not tag:
                raise ValueError("equipment tag cannot be empty")
            if len(tag) > 64:
                raise ValueError("equipment tag is too long")
            if tag not in normalized:
                normalized.append(tag)
        if not normalized:
            raise ValueError("tags must not be empty")
        return normalized


class EquipmentTagsIn(BaseModel):
    owner_school_id: str = Field(min_length=1, max_length=64)
    tags: list[str] = Field(min_length=1)

    @field_validator("tags")
    @classmethod
    def _tags_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not tag for tag in normalized):
            raise ValueError("equipment tag cannot be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("tags must be distinct")
        return normalized


class EquipmentSuspensionIn(BaseModel):
    owner_school_id: str = Field(min_length=1, max_length=64)
    start_at: datetime
    end_at: datetime
    reason: Optional[str] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _window_ordered(self) -> "EquipmentSuspensionIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class EquipmentLiftIn(BaseModel):
    owner_school_id: str = Field(min_length=1, max_length=64)


class EquipmentRequirementIn(BaseModel):
    tags: list[str] = Field(min_length=1)
    count: int = Field(ge=1, le=1_000_000)

    @field_validator("tags")
    @classmethod
    def _tags_normalized(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            tag = value.strip()
            if not tag:
                raise ValueError("requirement tag cannot be empty")
            if len(tag) > 64:
                raise ValueError("requirement tag is too long")
            if tag not in normalized:
                normalized.append(tag)
        if not normalized:
            raise ValueError("a requirement needs at least one tag")
        return normalized


class EquipmentReservationIn(BaseModel):
    reservation_id: Optional[str] = Field(
        default=None, min_length=1, max_length=64, pattern=ID_PATTERN
    )
    organizer_school_id: str = Field(min_length=1, max_length=64)
    term_id: str = Field(min_length=1, max_length=64)
    class_id: str = Field(min_length=1, max_length=64)
    start_at: datetime
    end_at: datetime
    requirements: list[EquipmentRequirementIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _window_ordered(self) -> "EquipmentReservationIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class EquipmentCancelIn(BaseModel):
    organizer_school_id: str = Field(min_length=1, max_length=64)


class EquipmentPickupIn(BaseModel):
    organizer_school_id: str = Field(min_length=1, max_length=64)
    item_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _item_ids_unique(self) -> "EquipmentPickupIn":
        if len(set(self.item_ids)) != len(self.item_ids):
            raise ValueError("item_ids must be distinct; one physical item cannot fill two slots")
        return self


class EquipmentReturnIn(BaseModel):
    organizer_school_id: str = Field(min_length=1, max_length=64)


class EquipmentRescheduleIn(BaseModel):
    organizer_school_id: str = Field(min_length=1, max_length=64)
    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def _window_ordered(self) -> "EquipmentRescheduleIn":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self
