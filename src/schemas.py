"""Pydantic data models for LLM output (raw) and validated evaluation results.

Two layers on purpose:
- Raw*    : what the LLM sent back, accepted leniently so one bad field does not
            throw away the whole response. Nothing here is trusted yet.
- *Result : what our validation produced after applying business rules. Only
            these objects are used for scoring, ranking and storage.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CriterionStatus = Literal["ASSESSED", "MISSING", "INVALID"]
EvaluationStatus = Literal["OK", "REVIEW_REQUIRED", "FAILED"]


def _as_str_list(value: Any) -> list[str]:
    """Accept None, a single string, or a list; return a clean list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("must be a list of strings")
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


class RawCriterionScore(BaseModel):
    """One criterion entry exactly as the LLM returned it (untrusted)."""

    model_config = ConfigDict(extra="ignore")

    criterion_id: int
    score: float | None = None
    max_score: int | None = None
    justification: str = ""
    evidence: list[str] = Field(default_factory=list)

    @field_validator("score", mode="before")
    @classmethod
    def _reject_bool_score(cls, value: Any) -> Any:
        # Python treats True as 1; a boolean is never a valid score.
        if isinstance(value, bool):
            raise ValueError("score must be a number, not true/false")
        return value

    @field_validator("justification", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("evidence", mode="before")
    @classmethod
    def _clean_evidence(cls, value: Any) -> list[str]:
        return _as_str_list(value)


class RawEvaluation(BaseModel):
    """The whole LLM response for one supplier (untrusted)."""

    model_config = ConfigDict(extra="ignore")

    criteria: list[RawCriterionScore]
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    overall_summary: str = ""

    @field_validator("risks", "missing_information", mode="before")
    @classmethod
    def _clean_lists(cls, value: Any) -> list[str]:
        return _as_str_list(value)

    @field_validator("overall_summary", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return "" if value is None else value


class CriterionResult(BaseModel):
    """One criterion after validation. `score` is 0 when status is not ASSESSED."""

    criterion_id: int
    name: str
    weight: float
    max_score: int
    score: int
    status: CriterionStatus
    justification: str
    evidence: list[str]


class EvaluationResult(BaseModel):
    """A supplier's validated evaluation, ready for scoring."""

    status: EvaluationStatus
    criteria: list[CriterionResult]
    risks: list[str]
    missing_information: list[str]
    overall_summary: str
    warnings: list[str]