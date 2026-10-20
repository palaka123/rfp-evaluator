"""Orchestrator: runs the evaluation workflow step by step and records what each tool did.

  Input validation -> load active criteria -> create RFP run -> for each supplier:
  [Document tool -> Evaluation agent -> Validation tool] -> Scoring -> Benchmarking
  -> Ranking -> Persist (one transaction) -> results

Failure policy:
- Invalid input or invalid weights: nothing starts; errors are returned to the user.
- One supplier's PDF is unreadable or its LLM answer stays invalid: that supplier is
  FAILED (unranked), the others continue, and the run is PARTIAL.
- A provider error that affects every supplier (bad key, unknown model, bad request):
  the run is stopped and marked FAILED, because continuing would only repeat the error.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.config import LLMSettings
from src.db import (
    CriteriaError,
    create_run,
    load_criteria,
    load_run,
    save_supplier_results,
    set_run_status,
    validate_active_weights,
)
from src.input_validation import SupplierSubmission, normalise_name, validate_submissions
from src.llm_evaluator import LLMProviderError, evaluate_proposal, failed_evaluation
from src.pdf_extractor import PDFExtractionError, extract_text
from src.ranking import TIE_BREAK_RULES, rank_suppliers
from src.scoring import ScoredSupplier, SupplierEvaluation, score_batch

ProgressCallback = Callable[[str], None]


class InputValidationError(ValueError):
    """The batch was rejected before starting. `errors` lists every problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class RunAbortedError(RuntimeError):
    """The run was stopped part-way (it is saved with status FAILED)."""

    def __init__(self, rfp_run_id: str, cause: LLMProviderError) -> None:
        super().__init__(f"Run {rfp_run_id} stopped: {cause}")
        self.rfp_run_id = rfp_run_id
        self.cause = cause


@dataclass
class RunOutcome:
    rfp_run_id: str
    status: str
    suppliers: list[ScoredSupplier]
    batch_warnings: list[str]
    steps: list[dict[str, Any]] = field(default_factory=list)


def new_run_id(now: datetime) -> str:
    """Readable and unique: RFP-20261020-013512-3f9a1c."""
    return f"RFP-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"


def decide_run_status(suppliers: list[ScoredSupplier]) -> str:
    ranked = sum(1 for s in suppliers if s.final_rank is not None)
    if ranked == 0:
        return "FAILED"
    return "COMPLETED" if ranked == len(suppliers) else "PARTIAL"


def run_evaluation(
    conn: sqlite3.Connection,
    submissions: list[SupplierSubmission],
    client: Any,
    settings: LLMSettings,
    progress: ProgressCallback | None = None,
) -> RunOutcome:
    """Run the full workflow for one batch. See the module docstring for the failure policy."""
    say = progress or (lambda message: None)
    steps: list[dict[str, Any]] = []

    def log(step: str, outcome: str, supplier: str | None = None, detail: str = "") -> None:
        steps.append({"step": step, "supplier": supplier, "outcome": outcome, "detail": detail})

    # 1. Input validation (nothing is saved for an invalid batch)
    errors = validate_submissions(submissions)
    if errors:
        raise InputValidationError(errors)
    for s in submissions:
        s.supplier_name = normalise_name(s.supplier_name)
    log("validate_inputs", "ok", detail=f"{len(submissions)} suppliers")

    # 2. Reload the latest active criteria and check the weights
    criteria = load_criteria(conn)
    validate_active_weights(criteria)  # raises CriteriaError
    log("load_criteria", "ok", detail=f"{len(criteria)} active criteria")

    # 3. Create the run with a snapshot of the criteria it uses
    now = datetime.now(timezone.utc)
    run_id = new_run_id(now)
    create_run(conn, run_id, now.isoformat(timespec="seconds"), criteria)
    log("create_run", "ok", detail=run_id)
    say(f"Started run {run_id} with {len(submissions)} suppliers.")

    # 4. Evaluate each supplier independently
    evaluations: list[SupplierEvaluation] = []
    details: dict[str, dict[str, Any]] = {}
    for index, s in enumerate(submissions, start=1):
        name = s.supplier_name
        info: dict[str, Any] = {"filename": s.filename}
        details[name] = info
        say(f"[{index}/{len(submissions)}] {name}: reading PDF")

        try:
            document = extract_text(s.pdf_bytes, s.filename)
        except PDFExtractionError as exc:
            log("extract_text", exc.kind, name, str(exc))
            result = failed_evaluation(criteria, f"PDF could not be used: {exc}")
        else:
            info.update(page_count=document.page_count, characters=document.char_count,
                        truncated=document.truncated)
            log("extract_text", "ok", name, f"{document.page_count} pages, {document.char_count:,} characters")
            say(f"[{index}/{len(submissions)}] {name}: evaluating with {settings.model}")
            try:
                result, trace = evaluate_proposal(client, settings, criteria, name, document.text)
            except LLMProviderError as exc:
                log("evaluate", exc.kind, name, str(exc))
                if not exc.retryable:
                    set_run_status(conn, run_id, "FAILED")
                    raise RunAbortedError(run_id, exc) from exc
                result = failed_evaluation(criteria, f"LLM provider error: {exc}")
            else:
                info["llm"] = {"model": trace.model, "attempts": trace.attempts,
                               "input_tokens": trace.input_tokens, "output_tokens": trace.output_tokens}
                log("evaluate", "ok", name, f"{len(trace.attempts)} attempt(s)")
                if document.warnings:
                    result = result.model_copy(update={"warnings": document.warnings + result.warnings})
            log("validate_output", result.status, name, f"{len(result.warnings)} warning(s)")

        evaluations.append(SupplierEvaluation(supplier_name=name, submission_date=s.submission_date,
                                              experience_rating=s.experience_rating, evaluation=result))

    # 5-7. Score, benchmark and rank (pure Python, deterministic)
    say("Calculating scores, benchmarks, PPI and ranking")
    scored, batch_warnings = score_batch(evaluations)
    ranked = rank_suppliers(scored)
    log("score_and_benchmark", "ok", detail=f"{len(batch_warnings)} batch warning(s)")
    log("rank", "ok", detail=f"{sum(1 for s in ranked if s.final_rank)} ranked")

    # 8. Persist everything in one transaction
    status = decide_run_status(ranked)
    log("persist", "ok", detail=f"run status {status}")
    rows = []
    for s in ranked:
        workflow = [st for st in steps if st["supplier"] in (None, s.supplier_name)]
        result_json = {**s.model_dump(mode="json"), "rfp_run_id": run_id, "run_status": status,
                       "document_and_llm": details[s.supplier_name], "batch_warnings": batch_warnings,
                       "workflow_steps": workflow}
        rows.append({
            "supplier_name": s.supplier_name, "submission_date": s.submission_date.isoformat(),
            "experience_rating": s.experience_rating, "absolute_score": s.absolute_score, "ppi": s.ppi,
            "final_rank": s.final_rank, "evaluation_status": s.status,
            "result_json": json.dumps(result_json),
        })
    save_supplier_results(conn, run_id, rows, status)
    say(f"Run {run_id} finished with status {status}.")
    return RunOutcome(rfp_run_id=run_id, status=status, suppliers=ranked,
                      batch_warnings=batch_warnings, steps=steps)


def build_run_export(conn: sqlite3.Connection, rfp_run_id: str) -> dict[str, Any]:
    """The complete, downloadable JSON for one run, read back from SQLite."""
    run = load_run(conn, rfp_run_id)
    if run is None:
        raise KeyError(f"Run {rfp_run_id} not found.")
    return {
        **run,
        "tie_break_rules": TIE_BREAK_RULES,
        "formulas": {
            "absolute_score": "sum((score / max_score) * weight)",
            "benchmark": "highest assessed score per criterion among non-FAILED suppliers",
            "gap": "score - benchmark",
            "relative_pct": "score / benchmark * 100 (0 when benchmark is 0)",
            "ppi": "sum(weight * relative_pct) / sum(weight); rounded half-up to 2 dp; ranking uses rounded PPI",
        },
        "note": "Synthetic proposals; supplier statements are unverified vendor claims.",
    }


__all__ = ["CriteriaError", "InputValidationError", "RunAbortedError", "RunOutcome",
           "build_run_export", "run_evaluation"]