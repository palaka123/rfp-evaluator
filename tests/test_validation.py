"""Tests for LLM output parsing and normalisation, using mock responses (no API calls)."""

import json

import pytest

from src.db import Criterion
from src.validation import (
    LLMResponseError,
    normalize_evaluation,
    parse_llm_response,
    round_half_up,
)

# Mock criteria matching the seeded defaults (IDs 1-5, max score 10).
CRITERIA = [
    Criterion(1, "Technical Capability", "d", 30.0, 10, True),
    Criterion(2, "Implementation Plan", "d", 20.0, 10, True),
    Criterion(3, "Commercial Value", "d", 20.0, 10, True),
    Criterion(4, "Security & Compliance", "d", 20.0, 10, True),
    Criterion(5, "Support & Experience", "d", 10.0, 10, True),
]


def entry(cid, score=7, evidence=("Quoted text from proposal",), justification="Reason.", max_score=10):
    return {
        "criterion_id": cid, "score": score, "max_score": max_score,
        "justification": justification, "evidence": list(evidence),
    }


def response(entries, summary="Overall summary.", risks=("A risk",)):
    """Build a mock LLM response string."""
    return json.dumps({
        "criteria": entries, "risks": list(risks),
        "missing_information": [], "overall_summary": summary,
    })


def evaluate(text):
    raw, parse_warnings = parse_llm_response(text)
    return normalize_evaluation(raw, CRITERIA, parse_warnings)


# --- Happy path ----------------------------------------------------------

def test_complete_valid_response_is_ok():
    result = evaluate(response([entry(i, score=i + 3) for i in range(1, 6)]))
    assert result.status == "OK"
    assert result.warnings == []
    assert [c.score for c in result.criteria] == [4, 5, 6, 7, 8]
    assert all(c.status == "ASSESSED" for c in result.criteria)
    assert result.criteria[0].weight == 30.0  # weight comes from the database, not the model


def test_genuine_zero_is_assessed_not_missing():
    result = evaluate(response([entry(1, score=0)] + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].score == 0
    assert result.criteria[0].status == "ASSESSED"
    assert result.status == "OK"


def test_code_fenced_json_is_accepted():
    text = "```json\n" + response([entry(i) for i in range(1, 6)]) + "\n```"
    assert evaluate(text).status == "OK"


# --- Unusable responses (retryable errors) -------------------------------

@pytest.mark.parametrize("text", [None, "", "   \n"])
def test_empty_response_raises(text):
    with pytest.raises(LLMResponseError) as info:
        parse_llm_response(text)
    assert info.value.kind == "empty"


def test_malformed_json_raises():
    with pytest.raises(LLMResponseError) as info:
        parse_llm_response('{"criteria": [ {"criterion_id": 1, ')
    assert info.value.kind == "malformed_json"


@pytest.mark.parametrize("text", ["[1, 2, 3]", '{"scores": []}', '{"criteria": "none"}'])
def test_wrong_top_level_shape_raises(text):
    with pytest.raises(LLMResponseError) as info:
        parse_llm_response(text)
    assert info.value.kind == "schema"


# --- Missing, unknown and duplicate criteria -----------------------------

def test_missing_criterion_marked_missing_and_review_required():
    result = evaluate(response([entry(i) for i in range(1, 5)]))  # ID 5 absent
    last = result.criteria[4]
    assert (last.status, last.score) == ("MISSING", 0)
    assert result.status == "REVIEW_REQUIRED"
    assert any("ID 5" in w and "MISSING" in w for w in result.warnings)


def test_null_score_means_not_assessable():
    result = evaluate(response([entry(1, score=None, justification="No pricing given.")]
                               + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].status == "MISSING"
    assert result.criteria[0].justification == "No pricing given."
    assert result.status == "REVIEW_REQUIRED"


def test_unknown_criterion_id_ignored_with_warning():
    result = evaluate(response([entry(i) for i in range(1, 6)] + [entry(99)]))
    assert len(result.criteria) == 5
    assert result.status == "OK"
    assert any("Unknown criterion ID 99" in w for w in result.warnings)


def test_duplicate_criterion_keeps_first():
    entries = [entry(1, score=8), entry(1, score=2)] + [entry(i) for i in range(2, 6)]
    result = evaluate(response(entries))
    assert result.criteria[0].score == 8
    assert any("Duplicate" in w for w in result.warnings)


def test_more_than_half_missing_fails():
    result = evaluate(response([entry(1), entry(2)]))  # 3 of 5 missing
    assert result.status == "FAILED"


def test_exactly_half_missing_is_not_failed():
    criteria4 = CRITERIA[:4]
    raw, w = parse_llm_response(response([entry(1), entry(2)]))  # 2 of 4 missing
    assert normalize_evaluation(raw, criteria4, w).status == "REVIEW_REQUIRED"


def test_unreadable_entry_dropped_and_becomes_missing():
    entries = [{"criterion_id": "abc", "score": 5}] + [entry(i) for i in range(2, 6)]
    result = evaluate(response(entries))
    assert result.criteria[0].status == "MISSING"
    assert any("entry #1 could not be read" in w for w in result.warnings)


# --- Score range and type ------------------------------------------------

@pytest.mark.parametrize("bad", [-1, 11, 10.4])
def test_out_of_range_score_is_invalid(bad):
    result = evaluate(response([entry(1, score=bad)] + [entry(i) for i in range(2, 6)]))
    assert (result.criteria[0].status, result.criteria[0].score) == ("INVALID", 0)
    assert result.status == "REVIEW_REQUIRED"


def test_nan_score_is_invalid():
    text = response([entry(i) for i in range(2, 6)]).replace(
        '"criteria": [', '"criteria": [{"criterion_id": 1, "score": NaN, "evidence": ["x"], "justification": "j"}, '
    )
    assert evaluate(text).criteria[0].status == "INVALID"


def test_boolean_score_rejected():
    result = evaluate(response([entry(1, score=True)] + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].status == "MISSING"


def test_fractional_score_rounded_half_up_with_warning():
    result = evaluate(response([entry(1, score=7.5), entry(2, score=6.4)] + [entry(i) for i in range(3, 6)]))
    assert [result.criteria[0].score, result.criteria[1].score] == [8, 6]
    assert sum("rounded" in w for w in result.warnings) == 2
    assert result.status == "OK"


def test_round_half_up_differs_from_python_round():
    assert round(2.5) == 2          # Python's banker's rounding
    assert round_half_up(2.5) == 3  # what we use


def test_model_max_score_mismatch_uses_database_value():
    result = evaluate(response([entry(1, max_score=5)] + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].max_score == 10
    assert any("max score 5" in w for w in result.warnings)


# --- Evidence and justification ------------------------------------------

def test_missing_evidence_requires_review():
    result = evaluate(response([entry(1, evidence=[])] + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].status == "ASSESSED"
    assert result.status == "REVIEW_REQUIRED"
    assert any("no supporting evidence" in w for w in result.warnings)


def test_missing_justification_requires_review():
    result = evaluate(response([entry(1, justification="  ")] + [entry(i) for i in range(2, 6)]))
    assert result.status == "REVIEW_REQUIRED"


def test_evidence_given_as_single_string_is_accepted():
    e = entry(1)
    e["evidence"] = "Single quote"
    result = evaluate(response([e] + [entry(i) for i in range(2, 6)]))
    assert result.criteria[0].evidence == ["Single quote"]


def test_missing_summary_adds_warning():
    result = evaluate(response([entry(i) for i in range(1, 6)], summary=""))
    assert any("no overall summary" in w for w in result.warnings)


def test_result_serialises_to_json():
    result = evaluate(response([entry(i) for i in range(1, 6)]))
    restored = json.loads(result.model_dump_json())
    assert restored["status"] == "OK" and len(restored["criteria"]) == 5