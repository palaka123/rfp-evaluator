"""UI smoke tests with Streamlit's AppTest: the app renders every tab without errors.

A database is pre-filled with one run made by the fake LLM (with one FAILED supplier),
so the Results tab has something to show. No network calls are made.
"""

import pytest
from streamlit.testing.v1 import AppTest

from src.db import get_connection, init_db, load_run
from src.presentation import criteria_rows, leaderboard_rows, suggested_name
from src.orchestrator import run_evaluation
from tests.test_orchestrator import SETTINGS, FakeLLM, submissions


@pytest.fixture
def db_with_run(tmp_path, monkeypatch, pdfs):
    path = tmp_path / "ui.db"
    conn = get_connection(init_db(path))
    outcome = run_evaluation(conn, submissions(pdfs, broken="Orbit Digital"), FakeLLM(), SETTINGS)
    conn.close()
    monkeypatch.setenv("RFP_DB_PATH", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "")
    return path, outcome.rfp_run_id


@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    from scripts.generate_proposals import PROPOSALS, build_proposal
    folder = tmp_path_factory.mktemp("ui_pdfs")
    return {spec["supplier"]: build_proposal(spec, folder).read_bytes() for spec in PROPOSALS}


def run_app():
    at = AppTest.from_file("../app.py", default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def test_app_renders_all_tabs_with_results(db_with_run):
    _, run_id = db_with_run
    at = run_app()
    assert len(at.tabs) == 4
    assert any(run_id in str(sb.value) for sb in at.selectbox)
    assert any("Leaderboard" in md.value for md in at.markdown)
    assert len(at.dataframe) >= 3       # leaderboard + scorecards
    assert any("Not ranked" in e.label or "not ranked" in e.label for e in at.expander)


def test_criteria_editor_shows_weight_total(db_with_run):
    at = run_app()
    assert any("total 100%" in s.value for s in at.success)


def test_sample_toggle_shows_metadata_fields_and_missing_key_error(db_with_run):
    at = run_app()
    at.toggle[0].set_value(True).run()
    names = [t.value for t in at.text_input]
    assert names == ["Apex Systems", "BrightPath Tech", "NexaWorks", "Orbit Digital"]
    evaluate = next(b for b in at.button if b.label == "Evaluate suppliers")
    evaluate.click().run()
    assert not at.exception
    assert any("OPENAI_API_KEY is not set" in e.value for e in at.error)


def test_duplicate_names_are_reported_in_ui(db_with_run):
    at = run_app()
    at.toggle[0].set_value(True).run()
    at.text_input[1].set_value("apex systems").run()
    next(b for b in at.button if b.label == "Evaluate suppliers").click().run()
    assert any("duplicate supplier name" in e.value for e in at.error)


# --- Presentation helpers -------------------------------------------------

def test_presentation_rows(db_with_run):
    path, run_id = db_with_run
    conn = get_connection(path)
    run = load_run(conn, run_id)
    conn.close()
    rows = leaderboard_rows(run)
    assert [r["Rank"] for r in rows] == ["1", "2", "3", "Not ranked"]
    failed = run["suppliers"][-1]
    assert criteria_rows(failed)[0]["Benchmark"] == "-"
    assert criteria_rows(run["suppliers"][0])[1]["Gap"] == "0"  # NexaWorks holds the Implementation benchmark

def test_streamlit_secrets_are_used_when_env_is_empty(db_with_run):
    at = AppTest.from_file("../app.py", default_timeout=30)
    at.secrets["OPENAI_API_KEY"] = "sk-test-from-secrets"
    at.run()
    assert not at.exception
    assert any("API key:** configured" in md.value for md in at.sidebar.markdown)

def test_suggested_name():
    assert suggested_name("nexaworks.pdf") == "NexaWorks"
    assert suggested_name("acme_corp-ltd.pdf") == "Acme Corp Ltd"