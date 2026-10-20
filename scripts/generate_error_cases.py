"""Create a file for the error-case demonstration.

Run from the project root:  python -m scripts.generate_error_cases
Output: data/error_cases/blank_scanned_proposal.pdf, a 2-page PDF with no
extractable text (like an image-only scan). Uploading it with the four real
proposals produces a PARTIAL run: that supplier is FAILED and not ranked.
"""

from pathlib import Path

import pymupdf

OUTPUT = Path("data/error_cases/blank_scanned_proposal.pdf")


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(72, 72, 523, 770), color=(0.8, 0.8, 0.8))  # a frame, but no text
    doc.save(str(OUTPUT))
    doc.close()
    print(f"Created {OUTPUT}")


if __name__ == "__main__":
    main()