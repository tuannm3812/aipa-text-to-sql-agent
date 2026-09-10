"""Shared gold-vs-generated comparison for the evaluation harness.

Both the Streamlit evaluation tab and `scripts/evaluate_text_to_sql.py` import
this. It previously existed as two identical private copies that could drift
apart, silently making the app and the CLI disagree about accuracy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CASES_PATH = Path("evaluation/cases.json")


def canonical_value(value: Any) -> str:
    """Normalise one cell so equivalent values compare equal.

    Numbers are rounded to two decimals so `10`, `10.0` and `10.004` match;
    everything else is stripped and lowercased.

    Args:
        value: A single cell from a result row.

    Returns:
        The canonical string form of the value.
    """
    if isinstance(value, (int, float)):
        return str(round(float(value), 2))
    text = str(value).strip()
    try:
        return str(round(float(text), 2))
    except ValueError:
        return text.lower()


def normalise_rows(rows: list[tuple[Any, ...]]) -> list[list[str]]:
    """Render rows as lists of plain strings, for display and serialisation."""
    return [[str(value) for value in row] for row in rows]


def rows_match(generated: list[tuple[Any, ...]], gold: list[tuple[Any, ...]]) -> bool:
    """Compare two result sets ignoring row order and numeric formatting.

    Row order is ignored because a question rarely constrains it, and a query
    that returns the right rows in a different order is correct.

    Args:
        generated: Rows produced by the model's SQL.
        gold: Rows produced by the reference SQL.

    Returns:
        True if the two sets contain the same canonical rows.
    """
    generated_rows = sorted(tuple(canonical_value(v) for v in row) for row in generated)
    gold_rows = sorted(tuple(canonical_value(v) for v in row) for row in gold)
    return generated_rows == gold_rows


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load evaluation cases, returning an empty list when the file is absent.

    Args:
        path: Path to the cases JSON file.

    Returns:
        The parsed cases, or `[]` if the file does not exist.
    """
    cases_path = Path(path)
    if not cases_path.is_file():
        return []
    parsed: list[dict[str, Any]] = json.loads(cases_path.read_text(encoding="utf-8"))
    return parsed
