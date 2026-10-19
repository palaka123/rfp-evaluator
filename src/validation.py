"""Validation tool: turn raw LLM text into a trustworthy EvaluationResult.

Step 1  parse_llm_response   - text -> RawEvaluation. Raises LLMResponseError for
                               problems worth retrying (empty, malformed JSON,
                               wrong top-level shape).
Step 2  normalize_evaluation - RawEvaluation + active criteria -> EvaluationResult.
                               Applies business rules and records a warning for
                               every change it makes. Never raises for content
                               problems; it downgrades the status instead.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal

from pydantic import ValidationError

from src.db import Criterion
from src.schemas import CriterionResult, EvaluationResult, RawCriterionScore, RawEvaluation


class LLMResponseError(Exception):
    """The LLM response cannot be used at all. `kind` says why; these are retryable."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "empty" | "malformed_json" | "schema"


def round_half_up(value: float) -> int:
    """Round to the nearest integer with .5 going up (Python's round() uses banker's rounding)."""
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _strip_code_fences(text: str) -> str:
    """Remove a ```json ... ``` wrapper if the model added one."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]  # drop the opening ``` or ```json line
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    location = ".".join(str(part) for part in err["loc"]) or "value"
    return f"{location}: {err['msg']}"


def parse_llm_response(text: str | None) -> tuple[RawEvaluation, list[str]]:
    """Parse raw model text into a RawEvaluation plus any parse-time warnings.

    A single unreadable criterion entry is dropped with a warning (that criterion
    will later be marked MISSING) rather than discarding the whole response.
    """
    if text is None or not text.strip():
        raise LLMResponseError("empty", "The model returned an empty response.")

    try:
        data = json.loads(_strip_code_fences(text))
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            "malformed_json", f"Response is not valid JSON (line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(data, dict):
        raise LLMResponseError("schema", "Response must be a JSON object.")
    items = data.get("criteria")
    if not isinstance(items, list):
        raise LLMResponseError("schema", "Response must contain a 'criteria' list.")

    warnings: list[str] = []
    entries: list[RawCriterionScore] = []
    for position, item in enumerate(items, start=1):
        try:
            entries.append(RawCriterionScore.model_validate(item))
        except ValidationError as exc:
            warnings.append(f"Criterion entry #{position} could not be read and was ignored ({_first_error(exc)}).")

    try:
        raw = RawEvaluation.model_validate({**data, "criteria": entries})
    except ValidationError as exc:
        raise LLMResponseError("schema", f"Response has an invalid field ({_first_error(exc)}).") from exc
    return raw, warnings


def _not_assessed(c: Criterion, status: str, justification: str) -> CriterionResult:
    return CriterionResult(
        criterion_id=c.criterion_id, name=c.name, weight=c.weight, max_score=c.max_score,
        score=0, status=status, justification=justification, evidence=[],
    )


def normalize_evaluation(
    raw: RawEvaluation,
    active_criteria: list[Criterion],
    parse_warnings: list[str] | None = None,
) -> EvaluationResult:
    """Apply business rules to a parsed response for one supplier.

    Rules (each change adds a warning):
    - unknown criterion IDs are ignored; duplicates keep the first entry
    - a criterion not returned, or returned with a null score -> MISSING (score 0)
    - a score that is not finite, below 0 or above max_score -> INVALID (score 0)
    - a fractional score is rounded half-up to an integer
    - a returned max_score different from the database value is ignored
    - an assessed criterion without justification or evidence -> needs review
    Status: FAILED if more than half the criteria are not assessed; otherwise
    REVIEW_REQUIRED if anything is missing, invalid or unsupported; otherwise OK.
    """
    warnings = list(parse_warnings or [])
    by_id = {c.criterion_id: c for c in active_criteria}

    chosen: dict[int, RawCriterionScore] = {}
    for item in raw.criteria:
        if item.criterion_id not in by_id:
            warnings.append(f"Unknown criterion ID {item.criterion_id} was ignored.")
        elif item.criterion_id in chosen:
            warnings.append(f"Duplicate entry for criterion ID {item.criterion_id}; the first one was kept.")
        else:
            chosen[item.criterion_id] = item

    results: list[CriterionResult] = []
    needs_review = False

    for c in active_criteria:
        label = f"'{c.name}' (ID {c.criterion_id})"
        item = chosen.get(c.criterion_id)

        if item is None:
            warnings.append(f"{label} was not returned by the model; marked MISSING.")
            results.append(_not_assessed(c, "MISSING", "Not assessed: the model returned no result."))
            continue

        if item.max_score is not None and item.max_score != c.max_score:
            warnings.append(
                f"{label}: model reported max score {item.max_score}; the configured {c.max_score} was used."
            )

        if item.score is None:
            warnings.append(f"{label}: the model could not assess this criterion; marked MISSING.")
            reason = item.justification.strip() or "Not assessed: insufficient information in the proposal."
            results.append(_not_assessed(c, "MISSING", reason))
            continue

        if not math.isfinite(item.score) or item.score < 0 or item.score > c.max_score:
            warnings.append(
                f"{label}: score {item.score:g} is outside 0-{c.max_score}; marked INVALID and not used."
            )
            results.append(_not_assessed(c, "INVALID", f"Invalid model score {item.score:g} was rejected."))
            continue

        score = round_half_up(item.score)
        if score != item.score:
            warnings.append(f"{label}: fractional score {item.score:g} was rounded to {score}.")

        justification = item.justification.strip()
        if not justification:
            warnings.append(f"{label}: no justification was given; review required.")
            needs_review = True
        if not item.evidence:
            warnings.append(f"{label}: no supporting evidence was quoted; review required.")
            needs_review = True

        results.append(CriterionResult(
            criterion_id=c.criterion_id, name=c.name, weight=c.weight, max_score=c.max_score,
            score=score, status="ASSESSED", justification=justification, evidence=item.evidence,
        ))

    if not raw.overall_summary.strip():
        warnings.append("The model returned no overall summary.")

    not_assessed = sum(1 for r in results if r.status != "ASSESSED")
    if not_assessed * 2 > len(results):
        status = "FAILED"
        warnings.append(
            f"{not_assessed} of {len(results)} criteria could not be assessed (more than half); "
            "evaluation FAILED and the supplier will not be ranked."
        )
    elif not_assessed or needs_review:
        status = "REVIEW_REQUIRED"
    else:
        status = "OK"

    return EvaluationResult(
        status=status, criteria=results, risks=raw.risks,
        missing_information=raw.missing_information,
        overall_summary=raw.overall_summary.strip(), warnings=warnings,
    )