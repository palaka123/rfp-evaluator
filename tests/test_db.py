"""Tests for the database layer: schema, seeding, loading and weight validation."""

import sqlite3

import pytest

from src.db import (
    CriteriaError,
    Criterion,
    get_connection,
    init_db,
    load_criteria,
    seed_default_criteria,
    update_criteria,
    validate_active_weights,
)


@pytest.fixture
def conn(tmp_path):
    """A fresh, seeded database in a temporary folder for each test."""
    db_path = init_db(tmp_path / "test.db")
    connection = get_connection(db_path)
    yield connection
    connection.close()


def make_criterion(cid: int, weight: float, active: bool = True, max_score: int = 10) -> Criterion:
    return Criterion(cid, f"C{cid}", "desc", weight, max_score, active)


# --- Seeding and loading -------------------------------------------------

def test_seed_creates_five_default_criteria(conn):
    criteria = load_criteria(conn)
    assert [c.name for c in criteria] == [
        "Technical Capability",
        "Implementation Plan",
        "Commercial Value",
        "Security & Compliance",
        "Support & Experience",
    ]
    assert [c.weight for c in criteria] == [30.0, 20.0, 20.0, 20.0, 10.0]
    assert all(c.max_score == 10 and c.is_active for c in criteria)


def test_default_weights_pass_validation(conn):
    validate_active_weights(load_criteria(conn))  # must not raise


def test_reseeding_does_not_duplicate_or_overwrite(conn):
    update_criteria(conn, {1: (40.0, True), 5: (0.0, False)})
    assert seed_default_criteria(conn) == 0
    all_criteria = load_criteria(conn, active_only=False)
    assert len(all_criteria) == 5
    assert all_criteria[0].weight == 40.0  # user's edit survived


def test_load_active_only_excludes_inactive(conn):
    update_criteria(conn, {1: (40.0, True), 5: (0.0, False)})
    active_ids = [c.criterion_id for c in load_criteria(conn)]
    assert active_ids == [1, 2, 3, 4]
    assert len(load_criteria(conn, active_only=False)) == 5


# --- Weight validation ---------------------------------------------------

def test_weights_not_totalling_100_are_rejected():
    criteria = [make_criterion(1, 50), make_criterion(2, 40)]
    with pytest.raises(CriteriaError, match="total 90"):
        validate_active_weights(criteria)


def test_inactive_criteria_are_ignored_in_total():
    criteria = [make_criterion(1, 60), make_criterion(2, 40), make_criterion(3, 25, active=False)]
    validate_active_weights(criteria)  # 60 + 40 = 100; inactive 25 ignored


def test_no_active_criteria_rejected():
    with pytest.raises(CriteriaError, match="No active criteria"):
        validate_active_weights([make_criterion(1, 100, active=False)])


def test_zero_weight_active_criterion_rejected():
    criteria = [make_criterion(1, 100), make_criterion(2, 0)]
    with pytest.raises(CriteriaError, match="greater than 0"):
        validate_active_weights(criteria)


def test_small_floating_point_drift_is_accepted():
    criteria = [make_criterion(1, 33.33), make_criterion(2, 33.33), make_criterion(3, 33.34)]
    validate_active_weights(criteria)


# --- Updating criteria (all-or-nothing) ----------------------------------

def test_invalid_update_is_rolled_back(conn):
    with pytest.raises(CriteriaError):
        update_criteria(conn, {1: (50.0, True)})  # would total 120
    assert load_criteria(conn)[0].weight == 30.0  # nothing was saved


def test_unknown_criterion_id_rejected(conn):
    with pytest.raises(CriteriaError, match="Unknown"):
        update_criteria(conn, {99: (10.0, True)})


def test_database_rejects_out_of_range_weight(conn):
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("UPDATE evaluation_criteria SET weight = 150 WHERE criterion_id = 1")