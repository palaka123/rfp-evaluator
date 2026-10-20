"""Input validation: check supplier metadata and uploaded files BEFORE any run starts.

Returns a list of human-readable problems. An empty list means the batch is valid.
Nothing is written to the database and no API call is made for an invalid batch.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from src.pdf_extractor import MAX_FILE_BYTES

MIN_SUPPLIERS = 2      # peer benchmarking needs at least two suppliers
MAX_SUPPLIERS = 10
MAX_NAME_LENGTH = 100
MIN_RATING, MAX_RATING = 1.0, 5.0


@dataclass
class SupplierSubmission:
    """One supplier as entered in the UI: metadata plus the uploaded PDF bytes."""

    supplier_name: str
    submission_date: date | None
    experience_rating: float | None
    filename: str
    pdf_bytes: bytes


def normalise_name(name: str) -> str:
    """Trim and collapse inner spaces: '  Apex   Systems ' -> 'Apex Systems'."""
    return re.sub(r"\s+", " ", name or "").strip()


def validate_submissions(submissions: list[SupplierSubmission], today: date | None = None) -> list[str]:
    """Return every problem found (not just the first), so the user can fix them all at once."""
    today = today or date.today()
    errors: list[str] = []

    if len(submissions) < MIN_SUPPLIERS:
        errors.append(f"Upload at least {MIN_SUPPLIERS} supplier proposals; peer ranking needs a comparison.")
    if len(submissions) > MAX_SUPPLIERS:
        errors.append(f"At most {MAX_SUPPLIERS} suppliers can be evaluated in one run.")

    seen_names: dict[str, str] = {}
    seen_files: dict[str, str] = {}
    for position, s in enumerate(submissions, start=1):
        name = normalise_name(s.supplier_name)
        label = f"Supplier {position} ({name or s.filename or 'unnamed'})"

        if not name:
            errors.append(f"Supplier {position}: name is required.")
        elif len(name) > MAX_NAME_LENGTH:
            errors.append(f"{label}: name is longer than {MAX_NAME_LENGTH} characters.")
        elif name.casefold() in seen_names:
            errors.append(f"{label}: duplicate supplier name (same as '{seen_names[name.casefold()]}').")
        else:
            seen_names[name.casefold()] = name

        if s.submission_date is None:
            errors.append(f"{label}: submission date is required.")
        elif s.submission_date > today:
            errors.append(f"{label}: submission date {s.submission_date.isoformat()} is in the future.")

        if s.experience_rating is None:
            errors.append(f"{label}: experience rating is required.")
        elif not (MIN_RATING <= s.experience_rating <= MAX_RATING):
            errors.append(f"{label}: experience rating must be between {MIN_RATING} and {MAX_RATING}.")
        elif round(s.experience_rating, 1) != s.experience_rating:
            errors.append(f"{label}: experience rating must have at most one decimal place.")

        if not s.filename.lower().endswith(".pdf"):
            errors.append(f"{label}: file '{s.filename}' is not a .pdf file.")
        if not s.pdf_bytes:
            errors.append(f"{label}: file '{s.filename}' is empty.")
        elif len(s.pdf_bytes) > MAX_FILE_BYTES:
            errors.append(f"{label}: file is larger than {MAX_FILE_BYTES // 1_048_576} MB.")
        else:
            digest = hashlib.sha256(s.pdf_bytes).hexdigest()
            if digest in seen_files:
                errors.append(f"{label}: the same file was already uploaded for {seen_files[digest]}.")
            else:
                seen_files[digest] = name or s.filename

    return errors