"""Document tool: read an uploaded PDF and return clean, size-limited text.

The PDF is treated as untrusted input. We check size, file signature, encryption
and page count before reading, refuse documents with too little text (blank or
scanned), and cap the text length so one huge document cannot exhaust the LLM
context or the budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PAGES = 30
MAX_TEXT_CHARS = 60_000            # roughly 15k tokens
MIN_TEXT_CHARS = 200               # less than this is treated as empty / scanned


class PDFExtractionError(Exception):
    """The PDF cannot be used. `kind` says why; these are not retryable."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # empty_file | too_large | not_pdf | unreadable | encrypted | too_many_pages | no_text


@dataclass
class ExtractedDocument:
    text: str
    page_count: int
    char_count: int
    truncated: bool
    warnings: list[str] = field(default_factory=list)


def clean_text(text: str) -> str:
    """Normalise whitespace: no NULs or non-breaking spaces, single spaces, at most one blank line."""
    text = text.replace("\x00", "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(
    data: bytes,
    filename: str = "document.pdf",
    max_bytes: int = MAX_FILE_BYTES,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_TEXT_CHARS,
) -> ExtractedDocument:
    """Extract text from PDF bytes, or raise PDFExtractionError explaining why it cannot be used."""
    if not data:
        raise PDFExtractionError("empty_file", f"{filename}: the file is empty.")
    if len(data) > max_bytes:
        raise PDFExtractionError(
            "too_large", f"{filename}: file is {len(data) / 1_048_576:.1f} MB; the limit is "
                         f"{max_bytes / 1_048_576:.0f} MB.")
    if not data.lstrip()[:5].startswith(b"%PDF"):
        raise PDFExtractionError("not_pdf", f"{filename}: this is not a PDF file.")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise PDFExtractionError("unreadable", f"{filename}: the PDF is damaged or unreadable.") from exc

    try:
        if doc.needs_pass:
            raise PDFExtractionError("encrypted", f"{filename}: the PDF is password-protected.")
        if doc.page_count == 0:
            raise PDFExtractionError("unreadable", f"{filename}: the PDF has no pages.")
        if doc.page_count > max_pages:
            raise PDFExtractionError(
                "too_many_pages", f"{filename}: {doc.page_count} pages; the limit is {max_pages}.")

        pages = []
        for number, page in enumerate(doc, start=1):
            page_text = clean_text(page.get_text("text"))
            if page_text:
                pages.append(f"[Page {number}]\n{page_text}")
        page_count = doc.page_count
    except PDFExtractionError:
        raise
    except (RuntimeError, ValueError) as exc:
        raise PDFExtractionError("unreadable", f"{filename}: text could not be read from the PDF.") from exc
    finally:
        doc.close()

    text = "\n\n".join(pages)
    if len(text) < MIN_TEXT_CHARS:
        raise PDFExtractionError(
            "no_text", f"{filename}: almost no text was found. It may be blank or a scanned image.")

    warnings: list[str] = []
    truncated = len(text) > max_chars
    if truncated:
        warnings.append(f"{filename}: text was {len(text):,} characters; only the first "
                        f"{max_chars:,} were evaluated.")
        text = text[:max_chars]
    return ExtractedDocument(text=text, page_count=page_count, char_count=len(text),
                             truncated=truncated, warnings=warnings)