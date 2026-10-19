"""Scoring and benchmarking tools: all arithmetic is plain, deterministic Python.

Formulas (from the assignment):
  weighted score (per criterion) = (score / max_score) * weight
  absolute score                 = sum of weighted scores                  (0-100)
  benchmark (per criterion)      = highest ASSESSED score in the batch, among
                                   suppliers that did not FAIL
  gap                            = supplier score - benchmark              (score units)
  relative %                     = supplier score / benchmark * 100
                                   (benchmark 0 -> 0% for every supplier, plus a warning)
  PPI                            = sum(weight * relative %) / sum(weight)   (all active weights)

Intermediate values keep full precision. Only the final absolute score and PPI
are rounded (half-up, 2 decimals); ranking uses the rounded PPI.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel

from src.schemas import CriterionResult, EvaluationResult


class SupplierEvaluation(BaseModel):
    """Scoring input: one supplier's metadata plus its validated evaluation."""

    supplier_name: str
    submission_date: date
    experience_rating: float
    evaluation: EvaluationResult


class CriterionScore(BaseModel):
    """One criterion with its peer comparison. Peer fields are None for FAILED suppliers."""

    criterion_id: int
    name: str
    weight: float
    max_score: int
    score: int
    status: str
    justification: str
    evidence: list[str]
    weighted_score: float
    benchmark: int | None = None
    gap: int | None = None
    relative_pct: float | None = None


class ScoredSupplier(BaseModel):
    """A supplier after scoring. absolute_score/ppi are None when the evaluation FAILED."""

    supplier_name: str
    submission_date: date
    experience_rating: float
    status: str
    absolute_score: float | None
    ppi: float | None
    ppi_unrounded: float | None
    criteria: list[CriterionScore]
    risks: list[str]
    missing_information: list[str]
    overall_summary: str
    warnings: list[str]
    final_rank: int | None = None
    tie_break_notes: list[str] = []


def round2(value: float) -> float:
    """Round half-up to 2 decimals. round(value, 9) first removes float noise like 85.00499999."""
    return float(Decimal(str(round(value, 9))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def weighted_score(c: CriterionResult) -> float:
    return c.score / c.max_score * c.weight


def absolute_weighted_score(criteria: list[CriterionResult]) -> float:
    """Sum of (score / max) * weight. Not-assessed criteria contribute 0. Full precision."""
    return sum(weighted_score(c) for c in criteria)


def compute_benchmarks(evaluations: list[EvaluationResult]) -> dict[int, int]:
    """Highest ASSESSED score per criterion among non-FAILED evaluations (0 if none)."""
    benchmarks: dict[int, int] = {}
    for ev in evaluations:
        if ev.status == "FAILED":
            continue
        for c in ev.criteria:
            current = benchmarks.setdefault(c.criterion_id, 0)
            if c.status == "ASSESSED" and c.score > current:
                benchmarks[c.criterion_id] = c.score
    return benchmarks


def relative_performance(score: int, benchmark: int) -> float:
    """score / benchmark * 100, or 0.0 when the benchmark is 0."""
    return 0.0 if benchmark == 0 else score / benchmark * 100


def peer_performance_index(criteria: list[CriterionResult], benchmarks: dict[int, int]) -> float:
    """Weighted average of relative %; the denominator is the sum of all active weights."""
    total_weight = sum(c.weight for c in criteria)
    weighted = sum(c.weight * relative_performance(c.score, benchmarks[c.criterion_id]) for c in criteria)
    return weighted / total_weight


def _check_same_criteria(suppliers: list[SupplierEvaluation]) -> None:
    expected = [c.criterion_id for c in suppliers[0].evaluation.criteria]
    for s in suppliers[1:]:
        if [c.criterion_id for c in s.evaluation.criteria] != expected:
            raise ValueError(
                f"Supplier '{s.supplier_name}' was evaluated on different criteria; "
                "all suppliers in a batch must use the same criteria."
            )


def score_batch(suppliers: list[SupplierEvaluation]) -> tuple[list[ScoredSupplier], list[str]]:
    """Score every supplier against its peers. Returns (scored suppliers, batch warnings)."""
    if not suppliers:
        return [], []
    _check_same_criteria(suppliers)

    benchmarks = compute_benchmarks([s.evaluation for s in suppliers])
    batch_warnings: list[str] = []
    if not benchmarks:
        batch_warnings.append("No supplier evaluation succeeded; nothing can be ranked.")
    for c in suppliers[0].evaluation.criteria:
        if benchmarks.get(c.criterion_id) == 0:
            batch_warnings.append(
                f"Benchmark for '{c.name}' is 0 (no supplier scored above 0); "
                "relative performance is 0% for every supplier on this criterion."
            )

    scored: list[ScoredSupplier] = []
    for s in suppliers:
        ev = s.evaluation
        failed = ev.status == "FAILED"
        rows = []
        for c in ev.criteria:
            row = CriterionScore(**c.model_dump(), weighted_score=weighted_score(c))
            if not failed:
                bench = benchmarks[c.criterion_id]
                row.benchmark = bench
                row.gap = c.score - bench
                row.relative_pct = relative_performance(c.score, bench)
            rows.append(row)

        ppi_exact = None if failed else peer_performance_index(ev.criteria, benchmarks)
        scored.append(ScoredSupplier(
            supplier_name=s.supplier_name,
            submission_date=s.submission_date,
            experience_rating=s.experience_rating,
            status=ev.status,
            absolute_score=None if failed else round2(absolute_weighted_score(ev.criteria)),
            ppi=None if ppi_exact is None else round2(ppi_exact),
            ppi_unrounded=ppi_exact,
            criteria=rows,
            risks=ev.risks,
            missing_information=ev.missing_information,
            overall_summary=ev.overall_summary,
            warnings=ev.warnings,
        ))
    return scored, batch_warnings