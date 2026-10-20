"""Live smoke test: evaluate ONE proposal PDF with the real LLM and print the result.

Run from the project root, for example:
    python -m scripts.evaluate_one data/proposals/nexaworks.pdf "NexaWorks"

This makes one paid API call (a few cents at most). Nothing is saved to the database.
"""

import sys
from pathlib import Path

from src.config import get_llm_settings
from src.db import get_connection, init_db, load_criteria, validate_active_weights
from src.llm_evaluator import LLMProviderError, create_client, evaluate_proposal
from src.pdf_extractor import PDFExtractionError, extract_text


def main() -> int:
    if len(sys.argv) != 3:
        print('Usage: python -m scripts.evaluate_one <pdf path> "<supplier name>"')
        return 2
    pdf_path, supplier = Path(sys.argv[1]), sys.argv[2]

    conn = get_connection(init_db())
    try:
        criteria = load_criteria(conn)
    finally:
        conn.close()
    validate_active_weights(criteria)

    try:
        document = extract_text(pdf_path.read_bytes(), pdf_path.name)
        settings = get_llm_settings()
        print(f"Model: {settings.model} | {document.page_count} pages, {document.char_count:,} characters")
        result, trace = evaluate_proposal(create_client(settings), settings, criteria, supplier, document.text)
    except (PDFExtractionError, LLMProviderError) as exc:
        print(f"ERROR ({exc.kind}): {exc}")
        return 1

    print(f"\nStatus: {result.status} | attempts: {[a['outcome'] for a in trace.attempts]} | "
          f"tokens in/out: {trace.input_tokens}/{trace.output_tokens}\n")
    for c in result.criteria:
        print(f"[{c.criterion_id}] {c.name}: {c.score}/{c.max_score} ({c.status})")
        print(f"    Why: {c.justification}")
        for quote in c.evidence:
            print(f"    Evidence: {quote}")
    print(f"\nSummary: {result.overall_summary}")
    for label, items in (("Risks", result.risks), ("Missing information", result.missing_information),
                         ("Warnings", result.warnings)):
        print(f"\n{label}:" + ("" if items else " none"))
        for item in items:
            print(f"  - {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())