"""End-to-end workflow tests: real PDFs, real SQLite, fake LLM (no network, no cost)."""

import json
import re
from datetime import date
from types import SimpleNamespace

import httpx2
import openai
import pytest

from scripts.generate_proposals import PROPOSALS, build_proposal
from src.config import LLMSettings
from src.db import CriteriaError, get_connection, init_db, list_runs, load_run, update_criteria
from src.input_validation import SupplierSubmission, validate_submissions
from src.orchestrator import InputValidationError, RunAbortedError, build_run_export, run_evaluation

SETTINGS = LLMSettings(api_key="sk-test-not-real", model="fake-model", reasoning_effort="low")

# Fake scores per supplier for criteria 1-5 (default seeded weights 30/20/20/20/10).
FAKE_SCORES = {
    "Apex Systems": [9, 7, 5, 9, 6],
    "BrightPath Tech": [5, 5, 7, 3, 3],
    "NexaWorks": [7, 9, 7, 8, 9],
    "Orbit Digital": [6, 5, 6, 7, 8],
}
META = {  # submission date, experience rating
    "Apex Systems": (date(2026, 10, 5), 4.2),
    "BrightPath Tech": (date(2026, 10, 3), 2.1),
    "NexaWorks": (date(2026, 10, 4), 4.0),
    "Orbit Digital": (date(2026, 10, 6), 4.7),
}


def answer_for(prompt, override=None):
    name = re.search(r"Supplier name: (.+)", prompt).group(1).strip()
    ids = [int(i) for i in re.findall(r"criterion_id (\d+):", prompt)]
    scores = (override or FAKE_SCORES)[name]
    return json.dumps({
        "criteria": [{"criterion_id": cid, "score": scores[cid - 1], "max_score": 10,
                      "justification": f"Fake justification for {name}.",
                      "evidence": [f'"quote" [Page 1]']} for cid in ids],
        "risks": [], "missing_information": [], "overall_summary": f"Fake summary for {name}.",
    })


class FakeLLM:
    """Answers based on the supplier name in the prompt. `behaviour` maps a name to a special outcome."""

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls = 0
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        name = re.search(r"Supplier name: (.+)", kwargs["input"]).group(1).strip()
        special = self.behaviour.get(name)
        if isinstance(special, Exception):
            raise special
        text = "not json" if special == "garbage" else answer_for(kwargs["input"])
        return SimpleNamespace(output_text=text, status="completed", output=[], incomplete_details=None,
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=300))


def http_response(status):
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    folder = tmp_path_factory.mktemp("pdfs")
    return {spec["supplier"]: build_proposal(spec, folder).read_bytes() for spec in PROPOSALS}


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(init_db(tmp_path / "rfp.db"))
    yield connection
    connection.close()


def submissions(pdfs, names=None, broken=None):
    names = names or list(FAKE_SCORES)
    result = []
    for name in names:
        submitted, rating = META[name]
        data = b"%PDF-1.7 broken" if name == broken else pdfs[name]
        result.append(SupplierSubmission(name, submitted, rating, f"{name}.pdf", data))
    return result


# --- Happy path ----------------------------------------------------------

def test_full_run_ranks_persists_and_exports(conn, pdfs):
    outcome = run_evaluation(conn, submissions(pdfs), FakeLLM(), SETTINGS)
    assert outcome.status == "COMPLETED"
    ranked = [s.supplier_name for s in outcome.suppliers]
    assert ranked[0] == "NexaWorks"  # highest fake scores
    assert [s.final_rank for s in outcome.suppliers] == [1, 2, 3, 4]

    stored = load_run(conn, outcome.rfp_run_id)
    assert stored["status"] == "COMPLETED"
    assert [s["supplier_name"] for s in stored["suppliers"]] == ranked
    first = stored["suppliers"][0]
    assert first["criteria"][0]["evidence"] and first["criteria"][0]["benchmark"] is not None
    assert first["document_and_llm"]["llm"]["input_tokens"] == 1000
    assert {st["step"] for st in first["workflow_steps"]} >= {"extract_text", "evaluate", "rank", "persist"}
    assert len(stored["criteria_snapshot"]) == 5

    export = build_run_export(conn, outcome.rfp_run_id)
    assert export["tie_break_rules"] and json.dumps(export)  # serialisable


def test_columns_match_result_json(conn, pdfs):
    outcome = run_evaluation(conn, submissions(pdfs), FakeLLM(), SETTINGS)
    row = conn.execute("SELECT * FROM supplier_results WHERE final_rank = 1").fetchone()
    stored = json.loads(row["result_json"])
    assert row["ppi"] == stored["ppi"] and row["supplier_name"] == outcome.suppliers[0].supplier_name


# --- Partial failures ----------------------------------------------------

def test_unreadable_pdf_fails_one_supplier_only(conn, pdfs):
    outcome = run_evaluation(conn, submissions(pdfs, broken="Orbit Digital"), FakeLLM(), SETTINGS)
    assert outcome.status == "PARTIAL"
    orbit = next(s for s in outcome.suppliers if s.supplier_name == "Orbit Digital")
    assert orbit.status == "FAILED" and orbit.final_rank is None
    assert [s.final_rank for s in outcome.suppliers if s.final_rank] == [1, 2, 3]
    assert any("PDF could not be used" in w for w in orbit.warnings)


def test_invalid_llm_answers_fail_one_supplier(conn, pdfs):
    llm = FakeLLM({"Apex Systems": "garbage"})
    outcome = run_evaluation(conn, submissions(pdfs), llm, SETTINGS)
    apex = next(s for s in outcome.suppliers if s.supplier_name == "Apex Systems")
    assert apex.status == "FAILED" and outcome.status == "PARTIAL"
    assert llm.calls == 3 + 3  # 3 attempts for Apex + 1 each for the other three


def test_retryable_provider_error_fails_one_supplier(conn, pdfs):
    err = openai.RateLimitError("slow down", response=http_response(429), body=None)
    outcome = run_evaluation(conn, submissions(pdfs), FakeLLM({"NexaWorks": err}), SETTINGS)
    nexa = next(s for s in outcome.suppliers if s.supplier_name == "NexaWorks")
    assert nexa.status == "FAILED" and outcome.status == "PARTIAL"


def test_auth_error_aborts_run_and_marks_it_failed(conn, pdfs):
    err = openai.AuthenticationError("bad key", response=http_response(401), body=None)
    with pytest.raises(RunAbortedError) as info:
        run_evaluation(conn, submissions(pdfs), FakeLLM({"Apex Systems": err}), SETTINGS)
    stored = load_run(conn, info.value.rfp_run_id)
    assert stored["status"] == "FAILED" and stored["suppliers"] == []


# --- Rejected before starting --------------------------------------------

def test_invalid_input_creates_no_run(conn, pdfs):
    subs = submissions(pdfs, names=["Apex Systems", "NexaWorks"])
    subs[1].supplier_name = " apex   SYSTEMS "
    with pytest.raises(InputValidationError) as info:
        run_evaluation(conn, subs, FakeLLM(), SETTINGS)
    assert any("duplicate supplier name" in e for e in info.value.errors)
    assert list_runs(conn) == []


def test_invalid_weights_create_no_run(conn, pdfs):
    conn.execute("UPDATE evaluation_criteria SET weight = 50 WHERE criterion_id = 1")
    conn.commit()
    with pytest.raises(CriteriaError):
        run_evaluation(conn, submissions(pdfs), FakeLLM(), SETTINGS)
    assert list_runs(conn) == []


# --- History and dynamic criteria ----------------------------------------

def test_history_preserved_when_weights_change(conn, pdfs):
    first = run_evaluation(conn, submissions(pdfs), FakeLLM(), SETTINGS)
    update_criteria(conn, {1: (40.0, True), 5: (0.0, False)})  # 40/20/20/20, criterion 5 off
    second = run_evaluation(conn, submissions(pdfs), FakeLLM(), SETTINGS)

    old = load_run(conn, first.rfp_run_id)
    new = load_run(conn, second.rfp_run_id)
    assert [c["weight"] for c in old["criteria_snapshot"]] == [30.0, 20.0, 20.0, 20.0, 10.0]
    assert [c["weight"] for c in new["criteria_snapshot"]] == [40.0, 20.0, 20.0, 20.0]
    assert len(new["suppliers"][0]["criteria"]) == 4
    assert len(old["suppliers"][0]["criteria"]) == 5
    assert [r["rfp_run_id"] for r in list_runs(conn)][0] == second.rfp_run_id


# --- Input validation rules ----------------------------------------------

def sub(name="A", day=date(2026, 10, 1), rating=3.0, filename="a.pdf", data=b"%PDF-1"):
    return SupplierSubmission(name, day, rating, filename, data)


@pytest.mark.parametrize("bad, message", [
    (sub(name="  "), "name is required"),
    (sub(day=None), "submission date is required"),
    (sub(day=date(2030, 1, 1)), "in the future"),
    (sub(rating=5.5), "between 1.0 and 5.0"),
    (sub(rating=3.25), "one decimal"),
    (sub(rating=None), "experience rating is required"),
    (sub(filename="a.docx"), "not a .pdf"),
    (sub(data=b""), "is empty"),
])
def test_each_input_rule(bad, message):
    other = sub(name="Other", data=b"%PDF-other")
    errors = validate_submissions([bad, other], today=date(2026, 10, 20))
    assert any(message in e for e in errors), errors


def test_needs_at_least_two_suppliers():
    assert any("at least 2" in e for e in validate_submissions([sub()], today=date(2026, 10, 20)))


def test_same_file_twice_rejected():
    errors = validate_submissions([sub(name="A"), sub(name="B")], today=date(2026, 10, 20))
    assert any("same file" in e for e in errors)


def test_valid_batch_has_no_errors():
    batch = [sub(name="A", data=b"%PDF-a"), sub(name="B", data=b"%PDF-b")]
    assert validate_submissions(batch, today=date(2026, 10, 20)) == []