"""Request schemas. Responses are assembled as plain JSON-compatible dicts."""

from __future__ import annotations

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
