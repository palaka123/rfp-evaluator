"""Turn stored run data into display rows for the UI. Pure functions, so they are easy to test."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

# Suggested metadata for the four synthetic proposals (matches the dates printed on each PDF).
DEMO_METADATA: dict[str, tuple[str, date, float]] = {
    "apex_systems.pdf": ("Apex Systems", date(2026, 10, 5), 4.2),
    "brightpath_tech.pdf": ("BrightPath Tech", date(2026, 10, 3), 2.1),
    "nexaworks.pdf": ("NexaWorks", date(2026, 10, 4), 4.0),
    "orbit_digital.pdf": ("Orbit Digital", date(2026, 10, 6), 4.7),
}


def suggested_name(filename: str) -> str:
    """Known demo files get their real supplier name; others become 'Acme Corp' from 'acme_corp.pdf'."""
    if filename.lower() in DEMO_METADATA:
        return DEMO_METADATA[filename.lower()][0]
    return Path(filename).stem.replace("_", " ").replace("-", " ").strip().title()


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def leaderboard_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per supplier: ranked suppliers in rank order, then FAILED suppliers (unranked)."""
    return [{
        "Rank": str(s["final_rank"]) if s["final_rank"] is not None else "Not ranked",
        "Supplier": s["supplier_name"],
        "PPI": _fmt(s["ppi"]),
        "Absolute score": _fmt(s["absolute_score"]),
        "Status": s["status"],
        "Submitted": s["submission_date"],
        "Experience": f"{s['experience_rating']:.1f}",
        "Warnings": len(s["warnings"]),
    } for s in run["suppliers"]]


def criteria_rows(supplier: dict[str, Any]) -> list[dict[str, Any]]:
    """Criterion-wise scorecard: score, weight, weighted score, benchmark, gap and relative %."""
    rows = []
    for c in supplier["criteria"]:
        assessed = c["status"] == "ASSESSED"
        rows.append({
            "Criterion": c["name"],
            "Weight (%)": f"{c['weight']:g}",
            "Score": f"{c['score']}/{c['max_score']}" if assessed else f"not assessed ({c['status']})",
            "Weighted": _fmt(c["weighted_score"]),
            "Benchmark": "-" if c["benchmark"] is None else str(c["benchmark"]),
            "Gap": "-" if c["gap"] is None else ("0" if c["gap"] == 0 else f"{c['gap']:+d}"),
            "Relative (%)": _fmt(c["relative_pct"], 1),
        })
    return rows


def snapshot_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The criteria and weights this run actually used."""
    return [{"ID": c["criterion_id"], "Criterion": c["name"], "Weight (%)": f"{c['weight']:g}",
             "Max score": c["max_score"]} for c in run["criteria_snapshot"]]