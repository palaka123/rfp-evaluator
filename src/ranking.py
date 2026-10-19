"""Ranking tool: deterministic sort with the mandatory tie-break order.

  1. Higher PPI (the rounded, 2-decimal value that is displayed)
  2. Earlier submission date
  3. Higher historical experience rating
  4. Supplier name, A to Z (case-insensitive)

The full sort is applied first; ranks 1, 2, 3 ... are then assigned in order.
FAILED suppliers are never ranked: they get final_rank None and are listed last.
"""

from __future__ import annotations

from src.scoring import ScoredSupplier

TIE_BREAK_RULES = [
    "Higher Peer Performance Index (PPI) ranks first.",
    "If PPI is equal: earlier submission date ranks first.",
    "If still equal: higher historical experience rating ranks first.",
    "If still equal: supplier name in alphabetical order (A to Z).",
]


def sort_key(s: ScoredSupplier) -> tuple:
    """Smaller key = better rank. Negating PPI and experience turns 'higher first' into ascending order."""
    return (-s.ppi, s.submission_date, -s.experience_rating, s.supplier_name.casefold(), s.supplier_name)


def _deciding_reason(upper: ScoredSupplier, lower: ScoredSupplier) -> str:
    """Explain which tie-break separated two suppliers with the same PPI."""
    if upper.submission_date != lower.submission_date:
        return (f"earlier submission date ({upper.submission_date.isoformat()} vs "
                f"{lower.submission_date.isoformat()})")
    if upper.experience_rating != lower.experience_rating:
        return f"higher experience rating ({upper.experience_rating:.1f} vs {lower.experience_rating:.1f})"
    return "alphabetical order of supplier name"


def rank_suppliers(scored: list[ScoredSupplier]) -> list[ScoredSupplier]:
    """Return new objects: ranked suppliers in rank order, then FAILED suppliers (A to Z)."""
    rankable = sorted((s for s in scored if s.ppi is not None), key=sort_key)
    failed = sorted((s for s in scored if s.ppi is None), key=lambda s: s.supplier_name.casefold())

    ranked = [s.model_copy(update={"final_rank": i, "tie_break_notes": []}, deep=True)
              for i, s in enumerate(rankable, start=1)]

    for upper, lower in zip(ranked, ranked[1:]):
        if upper.ppi == lower.ppi:
            reason = _deciding_reason(upper, lower)
            upper.tie_break_notes.append(
                f"Tied with {lower.supplier_name} on PPI {upper.ppi:.2f}; ranked above by {reason}.")
            lower.tie_break_notes.append(
                f"Tied with {upper.supplier_name} on PPI {lower.ppi:.2f}; ranked below by {reason}.")

    unranked = [s.model_copy(update={"final_rank": None}, deep=True) for s in failed]
    return ranked + unranked