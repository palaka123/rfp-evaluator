"""Streamlit interface for the Agentic RFP Evaluation & Supplier Ranking system.

The UI only collects input and displays results. All business logic lives in src/.
Run locally:  streamlit run app.py
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from src.config import PLACEHOLDER_KEYS, LLMSettings, get_llm_settings
from src.db import (CriteriaError, get_connection, init_db, list_runs, load_criteria, load_run,
                    update_criteria)
from src.input_validation import SupplierSubmission, validate_submissions
from src.llm_evaluator import LLMProviderError, create_client
from src.orchestrator import InputValidationError, RunAbortedError, build_run_export, run_evaluation
from src.presentation import DEMO_METADATA, criteria_rows, leaderboard_rows, snapshot_rows, suggested_name
from src.ranking import TIE_BREAK_RULES

SAMPLE_DIR = Path("data/proposals")
STATUS_ICON = {"OK": "✅", "REVIEW_REQUIRED": "⚠️", "FAILED": "❌",
               "COMPLETED": "✅", "PARTIAL": "⚠️", "RUNNING": "⏳"}

SECRET_KEYS = ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT")

st.set_page_config(page_title="Agentic RFP Evaluator", page_icon="📑", layout="wide")


def load_secrets_into_env() -> None:
    """Streamlit Cloud keeps settings in st.secrets; copy them into environment variables,
    which is where src/config.py reads them. Locally there is usually no secrets file and
    the .env file is used instead."""
    try:
        for key in SECRET_KEYS:
            if key in st.secrets and not os.getenv(key):
                os.environ[key] = str(st.secrets[key])
    except FileNotFoundError:  # no secrets.toml: normal when running locally with .env
        return


# --- Sidebar --------------------------------------------------------------

def active_settings() -> LLMSettings:
    """Use the visitor's own API key if they entered one; otherwise the app's configured key (if any)."""
    settings = get_llm_settings()
    user_key = st.session_state.get("user_api_key", "").strip()
    return replace(settings, api_key=user_key) if user_key else settings


def render_sidebar() -> None:
    with st.sidebar:
        st.header("About")
        st.write("Evaluates supplier proposals for an **Enterprise AI Platform & Implementation RFP** "
                 "with an LLM, then scores and ranks them with deterministic Python.")
        st.caption("All proposals are **synthetic**. Supplier statements are unverified vendor claims.")
        st.divider()
        st.text_input("Your OpenAI API key", type="password", key="user_api_key", placeholder="sk-...",
                      help="Used only during this browser session to call OpenAI. It is not saved to the "
                           "database, written to logs or included in results.")
        settings = active_settings()
        if st.session_state.get("user_api_key", "").strip():
            key_status = "your key (this session only)"
        elif settings.api_key not in PLACEHOLDER_KEYS:
            key_status = "app key configured"
        else:
            key_status = "missing: enter your key above"
        st.write(f"**Model:** `{settings.model}`")
        st.write(f"**API key:** {key_status}")
        st.caption("On Streamlit Community Cloud the database is reset when the app restarts.")


# --- Tab 1: criteria ------------------------------------------------------

def render_criteria(conn) -> None:
    st.subheader("Evaluation criteria and weights")
    st.write("Loaded from SQLite. Change weights or switch criteria on/off, then save. "
             "The evaluation prompt is rebuilt from these values at the start of every run.")
    criteria = load_criteria(conn, active_only=False)
    table = pd.DataFrame([{
        "ID": c.criterion_id, "Criterion": c.name, "Description": c.description,
        "Weight (%)": c.weight, "Max score": c.max_score, "Active": c.is_active,
    } for c in criteria])

    edited = st.data_editor(
        table, hide_index=True, width="stretch", key="criteria_editor",
        disabled=["ID", "Criterion", "Description", "Max score"],
        column_config={
            "Weight (%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=1.0, format="%.1f"),
            "Active": st.column_config.CheckboxColumn(),
            "Description": st.column_config.TextColumn(width="large"),
        },
    )
    edited["Weight (%)"] = edited["Weight (%)"].fillna(0.0)
    total = float(edited.loc[edited["Active"], "Weight (%)"].sum())
    if abs(total - 100.0) <= 0.01:
        st.success(f"Active weights total {total:g}%.")
    else:
        st.warning(f"Active weights total {total:g}%. They must total 100% before saving.")

    if st.button("Save criteria", type="primary"):
        changes = {int(row["ID"]): (float(row["Weight (%)"]), bool(row["Active"]))
                   for _, row in edited.iterrows()}
        try:
            update_criteria(conn, changes)
            st.success("Saved. The next evaluation run will use these criteria and weights.")
        except CriteriaError as exc:
            st.error(f"Not saved: {exc}")


# --- Tab 2: evaluate ------------------------------------------------------

def collect_files() -> list[tuple[str, bytes]]:
    use_samples = st.toggle("Use the 4 built-in synthetic proposals", value=False,
                            help="Loads the sample PDFs from data/proposals so you can try the app without files.")
    if use_samples:
        return [(p.name, p.read_bytes()) for p in sorted(SAMPLE_DIR.glob("*.pdf"))]
    uploads = st.file_uploader("Supplier proposal PDFs", type=["pdf"], accept_multiple_files=True)
    return [(u.name, u.getvalue()) for u in uploads or []]


def render_evaluate(conn) -> None:
    st.subheader("Evaluate supplier proposals")
    st.write("Upload one PDF per supplier and enter its details. At least two suppliers are needed "
             "for peer benchmarking.")
    files = collect_files()
    if not files:
        st.info("No proposals selected yet.")
        return

    submissions: list[SupplierSubmission] = []
    for index, (filename, data) in enumerate(files):
        demo = DEMO_METADATA.get(filename.lower())
        st.markdown(f"**{index + 1}. `{filename}`** ({len(data) / 1024:.0f} KB)")
        col_name, col_date, col_rating = st.columns([2, 1, 1])
        name = col_name.text_input("Supplier name", value=suggested_name(filename), key=f"name_{index}_{filename}")
        submitted = col_date.date_input("Submission date", value=demo[1] if demo else date.today(),
                                        max_value=date.today(), key=f"date_{index}_{filename}")
        rating = col_rating.number_input("Experience rating (1.0-5.0)", min_value=1.0, max_value=5.0,
                                         value=demo[2] if demo else 3.0, step=0.1, format="%.1f",
                                         key=f"rating_{index}_{filename}")
        submissions.append(SupplierSubmission(name, submitted, round(rating, 1), filename, data))

    if not st.button("Evaluate suppliers", type="primary"):
        return

    errors = validate_submissions(submissions)
    if errors:
        st.error("Please fix these problems before evaluating:\n\n" + "\n".join(f"- {e}" for e in errors))
        return

    settings = active_settings()
    try:
        client = create_client(settings)
        with st.status(f"Evaluating {len(submissions)} suppliers with {settings.model}...", expanded=True) as box:
            outcome = run_evaluation(conn, submissions, client, settings, progress=box.write)
            box.update(label=f"Run {outcome.rfp_run_id} finished: {outcome.status}", state="complete")
    except InputValidationError as exc:
        st.error("Input problems:\n\n" + "\n".join(f"- {e}" for e in exc.errors))
        return
    except CriteriaError as exc:
        st.error(f"Criteria problem: {exc} Fix the weights in the Criteria tab.")
        return
    except RunAbortedError as exc:
        st.error(f"The run was stopped and saved as FAILED. {exc.cause}")
        return
    except LLMProviderError as exc:
        if exc.kind == "config":
            st.error("No OpenAI API key available. Enter your key in the sidebar and press Evaluate again.")
        else:
            st.error(f"LLM provider problem ({exc.kind}): {exc}")
        return

    st.session_state["selected_run"] = outcome.rfp_run_id
    message = f"Run **{outcome.rfp_run_id}** finished with status **{outcome.status}**. Open the **Results** tab."
    (st.success if outcome.status == "COMPLETED" else st.warning)(message)


# --- Tab 3: results -------------------------------------------------------

def render_supplier_card(supplier: dict) -> None:
    rank = supplier["final_rank"]
    title = (f"#{rank} {supplier['supplier_name']}: PPI {supplier['ppi']:.2f}" if rank is not None
             else f"{supplier['supplier_name']}: not ranked")
    with st.expander(f"{STATUS_ICON.get(supplier['status'], '')} {title} ({supplier['status']})",
                     expanded=rank == 1):
        cols = st.columns(5)
        cols[0].metric("Rank", rank if rank is not None else "-")
        cols[1].metric("PPI", "-" if supplier["ppi"] is None else f"{supplier['ppi']:.2f}")
        cols[2].metric("Absolute score", "-" if supplier["absolute_score"] is None
                       else f"{supplier['absolute_score']:.2f}")
        cols[3].metric("Submitted", supplier["submission_date"])
        cols[4].metric("Experience", f"{supplier['experience_rating']:.1f}")

        if supplier["overall_summary"]:
            st.markdown(f"**Summary:** {supplier['overall_summary']}")
        for note in supplier["tie_break_notes"]:
            st.info(f"Tie-break: {note}")
        st.dataframe(pd.DataFrame(criteria_rows(supplier)), hide_index=True, width="stretch")

        st.markdown("##### Justifications and evidence")
        for c in supplier["criteria"]:
            st.markdown(f"**{c['name']}** ({c['score']}/{c['max_score']}, {c['status']}): {c['justification']}")
            for quote in c["evidence"]:
                st.markdown(f"> {quote}")

        left, right = st.columns(2)
        with left:
            st.markdown("##### Risks")
            st.markdown("\n".join(f"- {r}" for r in supplier["risks"]) or "None reported.")
        with right:
            st.markdown("##### Missing information")
            st.markdown("\n".join(f"- {m}" for m in supplier["missing_information"]) or "None reported.")

        if supplier["warnings"]:
            st.markdown("##### Validation warnings")
            for w in supplier["warnings"]:
                st.warning(w)

        st.markdown("##### Workflow steps (agent trace)")
        st.dataframe(pd.DataFrame(supplier["workflow_steps"]), hide_index=True, width="stretch")
        llm = supplier["document_and_llm"].get("llm")
        if llm:
            st.caption(f"Model {llm['model']} | attempts: {len(llm['attempts'])} | "
                       f"tokens in/out: {llm['input_tokens']}/{llm['output_tokens']}")


def render_results(conn) -> None:
    runs = list_runs(conn)
    if not runs:
        st.info("No evaluation runs yet. Start one in the Evaluate tab.")
        return

    ids = [r["rfp_run_id"] for r in runs]
    labels = {r["rfp_run_id"]: f"{r['rfp_run_id']} | {r['status']} | {r['supplier_count']} suppliers" for r in runs}
    selected = st.session_state.get("selected_run")
    run_id = st.selectbox("Evaluation run (RFP_RUN_ID)", ids, index=ids.index(selected) if selected in ids else 0,
                          format_func=labels.get)
    run = load_run(conn, run_id)

    cols = st.columns(3)
    cols[0].metric("Run status", f"{STATUS_ICON.get(run['status'], '')} {run['status']}")
    cols[1].metric("Suppliers", len(run["suppliers"]))
    cols[2].metric("Ranked", sum(1 for s in run["suppliers"] if s["final_rank"] is not None))
    st.caption(f"Run {run['rfp_run_id']} created {run['created_at'].replace('T', ' ')[:16]} UTC")

    if run["status"] == "FAILED" and not run["suppliers"]:
        st.error("This run was stopped before any results were saved (for example, an invalid API key).")
        return
    if run["status"] == "PARTIAL":
        st.warning("Some suppliers could not be evaluated. They are listed as 'Not ranked' and excluded "
                   "from benchmarks.")
    for warning in run["suppliers"][0].get("batch_warnings", []):
        st.warning(warning)

    st.markdown("#### Leaderboard")
    st.dataframe(pd.DataFrame(leaderboard_rows(run)), hide_index=True, width="stretch")
    ranked = [s for s in run["suppliers"] if s["ppi"] is not None]
    if ranked:
        chart = pd.DataFrame({"Supplier": [s["supplier_name"] for s in ranked], "PPI": [s["ppi"] for s in ranked]})
        st.bar_chart(chart, x="Supplier", y="PPI", horizontal=True, sort="-PPI")
    if not any(s["tie_break_notes"] for s in run["suppliers"]):
        st.caption("No PPI ties in this run: the ranking was decided by PPI alone.")

    st.download_button("Download complete results (JSON)", file_name=f"{run_id}.json", mime="application/json",
                       data=json.dumps(build_run_export(conn, run_id), indent=2), type="primary")
    with st.expander("Criteria and weights used in this run"):
        st.dataframe(pd.DataFrame(snapshot_rows(run)), hide_index=True)

    st.markdown("#### Supplier scorecards")
    for supplier in run["suppliers"]:
        render_supplier_card(supplier)


# --- Tab 4: method --------------------------------------------------------

def render_method() -> None:
    st.subheader("How scoring and ranking work")
    st.markdown("""
The LLM only **reads and judges** each proposal against the active criteria, quoting evidence.
Every number below is calculated in **Python**, so the same validated inputs always give the same ranking.

| Quantity | Formula |
|---|---|
| Weighted score | (score / max score) x weight |
| Absolute score (0-100) | sum of weighted scores |
| Benchmark | highest assessed score for the criterion in this run (failed suppliers excluded) |
| Gap | supplier score - benchmark |
| Relative performance | supplier score / benchmark x 100 (0% for everyone if the benchmark is 0) |
| Peer Performance Index (PPI) | sum(weight x relative %) / sum(active weights), rounded half-up to 2 decimals |
""")
    st.markdown("**Ranking order (applied in full before ranks 1, 2, 3... are assigned):**")
    st.markdown("\n".join(f"{i}. {rule}" for i, rule in enumerate(TIE_BREAK_RULES, start=1)))
    st.markdown("""
**Validation:** missing or unassessable criteria are marked MISSING (score 0 but shown as *not assessed*);
out-of-range scores are INVALID. A supplier with more than half its criteria not assessed, an unreadable
PDF, or no valid LLM answer after 3 attempts is **FAILED** and not ranked.

**Safeguards:** PDF text is treated as untrusted data (instructions inside proposals are ignored and
reported as risks); file size, page count and text length are limited; API keys are never shown or stored
in results.
""")


def main() -> None:
    st.title("📑 Agentic RFP Evaluation & Supplier Ranking")
    st.caption("Enterprise AI Platform & Implementation RFP | synthetic data for an academic exercise")
    load_secrets_into_env()
    render_sidebar()
    db_path = os.getenv("RFP_DB_PATH", "data/rfp.db")
    with closing(get_connection(init_db(db_path))) as conn:
        tab_eval, tab_results, tab_criteria, tab_method = st.tabs(
            ["Evaluate", "Results", "Criteria & weights", "How it works"])
        with tab_eval:
            render_evaluate(conn)
        with tab_results:
            render_results(conn)
        with tab_criteria:
            render_criteria(conn)
        with tab_method:
            render_method()


main()