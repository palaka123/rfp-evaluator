"""Tests for formulas, benchmarks, PPI and tie-break ranking, against hand-calculated values."""

from datetime import date

import pytest

from src.ranking import rank_suppliers
from src.schemas import CriterionResult, EvaluationResult
from src.scoring import (
    SupplierEvaluation,
    absolute_weighted_score,
    relative_performance,
    round2,
    score_batch,
)

# Two criteria keep the hand calculations easy to check: weights 60 / 40, max score 10.
WEIGHTS = {1: 60.0, 2: 40.0}


def crit(cid, score, status="ASSESSED"):
    return CriterionResult(criterion_id=cid, name=f"C{cid}", weight=WEIGHTS[cid], max_score=10,
                           score=score, status=status, justification="j", evidence=["e"])


def supplier(name, s1, s2, day=1, exp=3.0, status="OK", st1="ASSESSED", st2="ASSESSED"):
    ev = EvaluationResult(status=status, criteria=[crit(1, s1, st1), crit(2, s2, st2)],
                          risks=[], missing_information=[], overall_summary="s", warnings=[])
    return SupplierEvaluation(supplier_name=name, submission_date=date(2026, 10, day),
                              experience_rating=exp, evaluation=ev)


def run(*suppliers):
    scored, warnings = score_batch(list(suppliers))
    return {s.supplier_name: s for s in rank_suppliers(scored)}, warnings


# --- Hand-calculated example ---------------------------------------------
#        C1 (60%)  C2 (40%)   Absolute                 PPI
#   A       8         6       48 + 24 = 72.00          60*100 + 40*66.667  -> 86.67
#   B       6         9       36 + 36 = 72.00          60*75  + 40*100     -> 85.00
#   C       4         3       24 + 12 = 36.00          60*50  + 40*33.333  -> 43.33
#   Benchmarks: C1 = 8, C2 = 9

def test_hand_calculated_example():
    r, warnings = run(supplier("A", 8, 6), supplier("B", 6, 9), supplier("C", 4, 3))
    assert warnings == []
    assert [r[n].absolute_score for n in "ABC"] == [72.0, 72.0, 36.0]
    assert [r[n].ppi for n in "ABC"] == [86.67, 85.0, 43.33]
    assert [r[n].final_rank for n in "ABC"] == [1, 2, 3]


def test_benchmark_gap_and_relative_pct():
    r, _ = run(supplier("A", 8, 6), supplier("B", 6, 9))
    b_c1 = r["B"].criteria[0]
    assert (b_c1.benchmark, b_c1.gap, b_c1.relative_pct) == (8, -2, 75.0)
    a_c1 = r["A"].criteria[0]
    assert (a_c1.gap, a_c1.relative_pct) == (0, 100.0)  # benchmark holder
    assert r["A"].criteria[1].relative_pct == pytest.approx(66.6667, abs=1e-4)


def test_same_absolute_score_different_ppi():
    r, _ = run(supplier("A", 8, 6), supplier("B", 6, 9))
    assert r["A"].absolute_score == r["B"].absolute_score
    assert r["A"].ppi != r["B"].ppi  # PPI, not absolute score, decides the rank


def test_absolute_score_uses_max_score_and_weight():
    assert absolute_weighted_score([crit(1, 10), crit(2, 10)]) == 100.0
    assert absolute_weighted_score([crit(1, 0), crit(2, 5)]) == 20.0


def test_ppi_keeps_full_precision_until_final_rounding():
    r, _ = run(supplier("A", 8, 6), supplier("B", 6, 9))
    assert r["A"].ppi_unrounded == pytest.approx(86.66666667)
    assert r["A"].ppi == 86.67


# --- Zero benchmark and failed suppliers ---------------------------------

def test_zero_benchmark_gives_zero_percent_and_warning():
    r, warnings = run(supplier("A", 8, 0), supplier("B", 6, 0))
    assert r["A"].criteria[1].relative_pct == 0.0
    assert r["B"].criteria[1].relative_pct == 0.0
    assert any("Benchmark for 'C2' is 0" in w for w in warnings)
    assert r["A"].ppi == 60.0  # 60*100 + 40*0, divided by 100


def test_relative_performance_zero_benchmark():
    assert relative_performance(0, 0) == 0.0


def test_failed_supplier_not_ranked_and_excluded_from_benchmark():
    r, _ = run(supplier("A", 6, 6), supplier("Z", 10, 10, status="FAILED"))
    assert r["Z"].final_rank is None and r["Z"].ppi is None
    assert r["A"].criteria[0].benchmark == 6  # Z's 10 did not count
    assert r["A"].final_rank == 1


def test_missing_criterion_excluded_from_benchmark():
    # B's C2 is MISSING (score 0); benchmark comes from A only.
    r, _ = run(supplier("A", 5, 4), supplier("B", 7, 0, status="REVIEW_REQUIRED", st2="MISSING"))
    assert r["A"].criteria[1].benchmark == 4
    assert r["B"].final_rank is not None  # REVIEW_REQUIRED is still ranked


def test_all_failed_gives_warning_and_no_ranks():
    r, warnings = run(supplier("A", 1, 1, status="FAILED"))
    assert r["A"].final_rank is None
    assert any("nothing can be ranked" in w for w in warnings)


def test_mismatched_criteria_rejected():
    bad = supplier("B", 5, 5)
    bad.evaluation.criteria.pop()
    with pytest.raises(ValueError, match="different criteria"):
        score_batch([supplier("A", 5, 5), bad])


# --- Tie-break order -----------------------------------------------------
# Identical scores give identical PPI, so the tie-breaks decide.

def test_tie_broken_by_earlier_submission_date():
    r, _ = run(supplier("A", 5, 5, day=9), supplier("B", 5, 5, day=2))
    assert (r["B"].final_rank, r["A"].final_rank) == (1, 2)
    assert "earlier submission date" in r["B"].tie_break_notes[0]


def test_tie_broken_by_higher_experience():
    r, _ = run(supplier("A", 5, 5, exp=3.5), supplier("B", 5, 5, exp=4.2))
    assert (r["B"].final_rank, r["A"].final_rank) == (1, 2)
    assert "higher experience rating (4.2 vs 3.5)" in r["B"].tie_break_notes[0]


def test_tie_broken_by_name_case_insensitive():
    r, _ = run(supplier("beta", 5, 5), supplier("Alpha", 5, 5))
    assert (r["Alpha"].final_rank, r["beta"].final_rank) == (1, 2)
    assert "alphabetical" in r["Alpha"].tie_break_notes[0]


def test_ppi_beats_every_tie_breaker():
    # B has earlier date, higher experience and earlier name, but lower PPI.
    r, _ = run(supplier("Zed", 9, 9, day=20, exp=1.0), supplier("Abe", 8, 8, day=1, exp=5.0))
    assert r["Zed"].final_rank == 1
    assert r["Zed"].tie_break_notes == []


def test_full_tie_break_chain_and_sequential_ranks():
    r, _ = run(
        supplier("Delta", 5, 5, day=5, exp=4.0),
        supplier("Charlie", 5, 5, day=5, exp=4.0),   # same as Delta -> name decides
        supplier("Bravo", 5, 5, day=5, exp=4.5),     # higher experience than Charlie/Delta
        supplier("Alpha", 5, 5, day=7, exp=5.0),     # latest date -> last despite best experience
        supplier("Echo", 9, 9, day=30, exp=1.0),     # highest PPI -> first
    )
    order = sorted(r.values(), key=lambda s: s.final_rank)
    assert [s.supplier_name for s in order] == ["Echo", "Bravo", "Charlie", "Delta", "Alpha"]
    assert [s.final_rank for s in order] == [1, 2, 3, 4, 5]


def test_ranking_is_deterministic_regardless_of_input_order():
    items = [supplier("A", 5, 5, day=3), supplier("B", 5, 5, day=1), supplier("C", 7, 2)]
    first = [s.supplier_name for s in rank_suppliers(score_batch(items)[0])]
    second = [s.supplier_name for s in rank_suppliers(score_batch(items[::-1])[0])]
    assert first == second


# --- Rounding ------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [(86.666666, 86.67), (85.005, 85.01),
                                             (85.0049, 85.0), (43.334999999999994, 43.34)])
def test_round2_half_up(value, expected):
    assert round2(value) == expected