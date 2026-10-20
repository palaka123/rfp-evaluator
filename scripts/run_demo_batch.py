"""Live end-to-end run: evaluate the four synthetic proposals, print the leaderboard,
and save the complete run JSON to sample_output/ (a required deliverable).

Run from the project root:  python -m scripts.run_demo_batch
Makes four paid API calls (well under one US cent in total with gpt-6-luna).
"""

import json
import sys
from datetime import date
from pathlib import Path

from src.config import get_llm_settings
from src.db import CriteriaError, get_connection, init_db
from src.input_validation import SupplierSubmission
from src.llm_evaluator import LLMProviderError, create_client
from src.orchestrator import InputValidationError, RunAbortedError, build_run_export, run_evaluation

DEMO = [  # file, supplier name, submission date, experience rating
    ("apex_systems.pdf", "Apex Systems", date(2026, 10, 5), 4.2),
    ("brightpath_tech.pdf", "BrightPath Tech", date(2026, 10, 3), 2.1),
    ("nexaworks.pdf", "NexaWorks", date(2026, 10, 4), 4.0),
    ("orbit_digital.pdf", "Orbit Digital", date(2026, 10, 6), 4.7),
]


def main() -> int:
    folder = Path("data/proposals")
    batch = [SupplierSubmission(name, day, rating, file, (folder / file).read_bytes())
             for file, name, day, rating in DEMO]
    settings = get_llm_settings()
    conn = get_connection(init_db())
    try:
        outcome = run_evaluation(conn, batch, create_client(settings), settings, progress=print)
        export = build_run_export(conn, outcome.rfp_run_id)
    except InputValidationError as exc:
        print("Input problems:\n  - " + "\n  - ".join(exc.errors))
        return 1
    except (CriteriaError, LLMProviderError, RunAbortedError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        conn.close()

    print(f"\n{'Rank':<6}{'Supplier':<18}{'PPI':>8}{'Absolute':>10}  Status")
    for s in outcome.suppliers:
        rank = s.final_rank if s.final_rank is not None else "-"
        ppi = f"{s.ppi:.2f}" if s.ppi is not None else "-"
        absolute = f"{s.absolute_score:.2f}" if s.absolute_score is not None else "-"
        print(f"{rank!s:<6}{s.supplier_name:<18}{ppi:>8}{absolute:>10}  {s.status}")
        for note in s.tie_break_notes:
            print(f"      tie-break: {note}")
    for warning in outcome.batch_warnings:
        print(f"Batch warning: {warning}")

    out = Path("sample_output") / f"{outcome.rfp_run_id}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(export, indent=2), encoding="utf-8")
    print(f"\nRun status: {outcome.status}\nSaved complete results to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())