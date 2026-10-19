"""Create and seed the SQLite database, then print the criteria as a check.

Run from the project root:  python -m scripts.init_db
"""

from src.db import (
    CriteriaError,
    get_connection,
    init_db,
    load_criteria,
    validate_active_weights,
)


def main() -> None:
    db_path = init_db()
    print(f"Database ready: {db_path.resolve()}")

    conn = get_connection(db_path)
    try:
        criteria = load_criteria(conn, active_only=False)
    finally:
        conn.close()

    print(f"\n{'ID':<4}{'Criterion':<26}{'Weight':>8}{'Max':>6}  Active")
    for c in criteria:
        print(f"{c.criterion_id:<4}{c.name:<26}{c.weight:>7g}%{c.max_score:>6}  {'yes' if c.is_active else 'no'}")

    active_total = sum(c.weight for c in criteria if c.is_active)
    print(f"\nActive weight total: {active_total:g}%")
    try:
        validate_active_weights(criteria)
        print("Weight check: PASSED")
    except CriteriaError as exc:
        print(f"Weight check: FAILED - {exc}")


if __name__ == "__main__":
    main()