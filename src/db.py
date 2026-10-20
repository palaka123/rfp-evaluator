"""Database layer: schema, criteria seeding, loading and weight validation.

All SQL lives in this module. Every query uses parameter placeholders (?)
so user-supplied values can never change the meaning of a statement.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path(os.getenv("RFP_DB_PATH", "data/rfp.db"))
WEIGHT_TOTAL = 100.0
WEIGHT_TOLERANCE = 0.01  # allows for tiny floating-point drift, e.g. 33.33 + 33.33 + 33.34


class CriteriaError(ValueError):
    """Raised when criteria or weights are not usable for an evaluation."""


@dataclass(frozen=True)
class Criterion:
    """One evaluation criterion, as stored in the evaluation_criteria table."""

    criterion_id: int
    name: str
    description: str
    weight: float      # percentage points, e.g. 30.0 means 30%
    max_score: int     # top of the scoring scale, e.g. 10
    is_active: bool


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evaluation_criteria (
    criterion_id INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL UNIQUE,
    description  TEXT    NOT NULL,
    weight       REAL    NOT NULL CHECK (weight >= 0 AND weight <= 100),
    max_score    INTEGER NOT NULL CHECK (max_score > 0),
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS rfp_runs (
    rfp_run_id              TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    status                  TEXT NOT NULL
        CHECK (status IN ('RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED')),
    criteria_snapshot_json  TEXT
);

CREATE TABLE IF NOT EXISTS supplier_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    rfp_run_id         TEXT    NOT NULL REFERENCES rfp_runs(rfp_run_id),
    supplier_name      TEXT    NOT NULL,
    submission_date    TEXT    NOT NULL,
    experience_rating  REAL    NOT NULL CHECK (experience_rating >= 1.0 AND experience_rating <= 5.0),
    absolute_score     REAL,
    ppi                REAL,
    final_rank         INTEGER,
    evaluation_status  TEXT    NOT NULL
        CHECK (evaluation_status IN ('OK', 'REVIEW_REQUIRED', 'FAILED')),
    result_json        TEXT    NOT NULL,
    UNIQUE (rfp_run_id, supplier_name)
);
"""

DEFAULT_CRITERIA: list[tuple[str, str, float, int]] = [
    (
        "Technical Capability",
        "Fit of the proposed enterprise AI platform: architecture, RAG and enterprise "
        "search design, integrations with existing systems, scalability and model choices.",
        30.0,
        10,
    ),
    (
        "Implementation Plan",
        "Credibility of delivery: phased timeline, milestones, team structure and "
        "staffing, dependencies, and how delivery risks will be managed.",
        20.0,
        10,
    ),
    (
        "Commercial Value",
        "Pricing clarity and total cost of ownership: licence and services costs, "
        "stated assumptions, exclusions, and value relative to scope.",
        20.0,
        10,
    ),
    (
        "Security & Compliance",
        "Security controls, data privacy and residency, access control, audit "
        "logging, AI governance, and claimed certifications.",
        20.0,
        10,
    ),
    (
        "Support & Experience",
        "Post-go-live support model and SLAs, comparable enterprise AI projects, "
        "and customer references.",
        10.0,
        10,
    ),
]


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys on and rows accessible by column name."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist. Safe to run repeatedly."""
    conn.executescript(SCHEMA_SQL)


def seed_default_criteria(conn: sqlite3.Connection) -> int:
    """Insert the default criteria only if the table is empty.

    Returns the number of rows inserted (0 if criteria already existed), so a
    user's edited weights are never overwritten by re-running the seed.
    """
    existing = conn.execute("SELECT COUNT(*) FROM evaluation_criteria").fetchone()[0]
    if existing:
        return 0
    with conn:  # one transaction: all five rows are inserted, or none are
        conn.executemany(
            "INSERT INTO evaluation_criteria (name, description, weight, max_score, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            DEFAULT_CRITERIA,
        )
    return len(DEFAULT_CRITERIA)


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> Path:
    """Create the database file, tables and default criteria. Returns the path used."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(path)
    try:
        create_schema(conn)
        seed_default_criteria(conn)
    finally:
        conn.close()
    return path


def _row_to_criterion(row: sqlite3.Row) -> Criterion:
    return Criterion(
        criterion_id=row["criterion_id"],
        name=row["name"],
        description=row["description"],
        weight=float(row["weight"]),
        max_score=int(row["max_score"]),
        is_active=bool(row["is_active"]),
    )


def load_criteria(conn: sqlite3.Connection, active_only: bool = True) -> list[Criterion]:
    """Load criteria ordered by ID. By default only active criteria are returned."""
    sql = "SELECT * FROM evaluation_criteria"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY criterion_id"
    return [_row_to_criterion(row) for row in conn.execute(sql)]


def validate_active_weights(criteria: list[Criterion]) -> None:
    """Check the active criteria can be used for scoring; raise CriteriaError if not.

    Rules: at least one active criterion; every active weight greater than 0;
    every max_score greater than 0; active weights total 100 (within tolerance).
    """
    active = [c for c in criteria if c.is_active]
    if not active:
        raise CriteriaError("No active criteria. Activate at least one criterion.")

    for c in active:
        if c.weight <= 0:
            raise CriteriaError(
                f"Active criterion '{c.name}' has weight {c.weight}; it must be greater than 0. "
                "Deactivate it instead of giving it zero weight."
            )
        if c.max_score <= 0:
            raise CriteriaError(f"Criterion '{c.name}' has an invalid max score of {c.max_score}.")

    total = sum(c.weight for c in active)
    if not math.isclose(total, WEIGHT_TOTAL, abs_tol=WEIGHT_TOLERANCE):
        raise CriteriaError(
            f"Active criterion weights total {total:g}%; they must total {WEIGHT_TOTAL:g}%."
        )


def update_criteria(
    conn: sqlite3.Connection, changes: dict[int, tuple[float, bool]]
) -> list[Criterion]:
    """Apply weight/active changes as one all-or-nothing transaction.

    `changes` maps criterion_id -> (new_weight, new_is_active). The resulting
    active set is validated before committing; if it is invalid, nothing is
    saved and CriteriaError is raised. Returns the active criteria after saving.
    """
    known_ids = {c.criterion_id for c in load_criteria(conn, active_only=False)}
    unknown = set(changes) - known_ids
    if unknown:
        raise CriteriaError(f"Unknown criterion ID(s): {sorted(unknown)}")

    try:
        conn.execute("BEGIN")
        for criterion_id, (weight, is_active) in changes.items():
            conn.execute(
                "UPDATE evaluation_criteria SET weight = ?, is_active = ? WHERE criterion_id = ?",
                (float(weight), int(bool(is_active)), criterion_id),
            )
        validate_active_weights(load_criteria(conn, active_only=True))
        conn.execute("COMMIT")
    except (CriteriaError, sqlite3.Error):
        conn.execute("ROLLBACK")
        raise
    return load_criteria(conn, active_only=True)



# --- Runs and results (added in Step 9) -----------------------------------

def create_run(conn: sqlite3.Connection, rfp_run_id: str, created_at: str, criteria: list[Criterion]) -> None:
    """Insert a RUNNING run with a snapshot of the exact criteria and weights it uses."""
    snapshot = json.dumps([asdict(c) for c in criteria])
    with conn:
        conn.execute(
            "INSERT INTO rfp_runs (rfp_run_id, created_at, status, criteria_snapshot_json) VALUES (?, ?, 'RUNNING', ?)",
            (rfp_run_id, created_at, snapshot),
        )


def set_run_status(conn: sqlite3.Connection, rfp_run_id: str, status: str) -> None:
    with conn:
        conn.execute("UPDATE rfp_runs SET status = ? WHERE rfp_run_id = ?", (status, rfp_run_id))


def save_supplier_results(conn: sqlite3.Connection, rfp_run_id: str, rows: list[dict], run_status: str) -> None:
    """Insert all supplier rows and set the final run status in ONE transaction (all or nothing)."""
    with conn:
        conn.executemany(
            "INSERT INTO supplier_results (rfp_run_id, supplier_name, submission_date, experience_rating, "
            "absolute_score, ppi, final_rank, evaluation_status, result_json) "
            "VALUES (:rfp_run_id, :supplier_name, :submission_date, :experience_rating, "
            ":absolute_score, :ppi, :final_rank, :evaluation_status, :result_json)",
            [{**row, "rfp_run_id": rfp_run_id} for row in rows],
        )
        conn.execute("UPDATE rfp_runs SET status = ? WHERE rfp_run_id = ?", (run_status, rfp_run_id))


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    """All runs, newest first, with how many suppliers each has."""
    sql = ("SELECT r.rfp_run_id, r.created_at, r.status, COUNT(s.id) AS supplier_count "
           "FROM rfp_runs r LEFT JOIN supplier_results s ON s.rfp_run_id = r.rfp_run_id "
           "GROUP BY r.rfp_run_id ORDER BY r.created_at DESC, r.rowid DESC")
    return [dict(row) for row in conn.execute(sql)]


def load_run(conn: sqlite3.Connection, rfp_run_id: str) -> dict | None:
    """One run with its criteria snapshot and supplier results (ranked first, by rank)."""
    run = conn.execute("SELECT * FROM rfp_runs WHERE rfp_run_id = ?", (rfp_run_id,)).fetchone()
    if run is None:
        return None
    rows = conn.execute(
        "SELECT result_json FROM supplier_results WHERE rfp_run_id = ? "
        "ORDER BY final_rank IS NULL, final_rank, supplier_name", (rfp_run_id,)).fetchall()
    return {
        "rfp_run_id": run["rfp_run_id"],
        "created_at": run["created_at"],
        "status": run["status"],
        "criteria_snapshot": json.loads(run["criteria_snapshot_json"] or "[]"),
        "suppliers": [json.loads(r["result_json"]) for r in rows],
    }