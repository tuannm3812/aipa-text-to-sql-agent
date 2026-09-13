"""Shared gold-vs-generated comparison for the evaluation harness.

Both the Streamlit evaluation tab and `scripts/evaluate_text_to_sql.py` import
this. It previously existed as two identical private copies that could drift
apart, silently making the app and the CLI disagree about accuracy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .execution import execute_query
from .safety import is_safe_query
from .types import QueryResult

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


@dataclass(frozen=True)
class CaseScore:
    """How one evaluation case scored, for a generated result against gold.

    Attributes:
        executed: Whether the generated query itself came back without an error.
        row_match: Rows equal as strings, in order.
        value_match: Rows equal ignoring order and numeric formatting.
        exact_match: `row_match` and the column names agree.
    """

    executed: bool
    row_match: bool
    value_match: bool
    exact_match: bool


def score_case(result: QueryResult, gold_result: QueryResult) -> CaseScore:
    """Score one case, requiring both sides to have executed before any match.

    Both results must be error-free for a match to count. Without that guard a
    failed query compares `[]` against `[]` and scores as a perfect match — so
    an aborted or blocked query would *inflate* the reported accuracy. The
    Streamlit tab and `scripts/evaluate_text_to_sql.py` both call this so they
    cannot drift apart on what "correct" means.

    Args:
        result: The result of the generated (or gold, in baseline mode) SQL.
        gold_result: The result of the reference SQL.

    Returns:
        A `CaseScore`. `executed` reflects only the generated side, matching how
        both harnesses have always reported it; every match field requires both
        sides to have succeeded.
    """
    both_ok = result.error is None and gold_result.error is None
    row_match = both_ok and normalise_rows(result.rows) == normalise_rows(gold_result.rows)
    value_match = both_ok and rows_match(result.rows, gold_result.rows)
    return CaseScore(
        executed=result.error is None,
        row_match=row_match,
        value_match=value_match,
        exact_match=row_match and result.columns == gold_result.columns,
    )


def run_gold(case: dict[str, Any]) -> tuple[str, QueryResult]:
    """Run a case's reference SQL, refusing it if it is not read-only.

    Gold SQL comes from `evaluation/cases.json` rather than from a model, so
    this is a consistency guard rather than an injection defence — but a
    reference query that reads schema internals or writes is a broken case, and
    both harnesses should say so identically instead of one executing it and the
    other refusing.

    Args:
        case: An evaluation case; `gold_sql` and `db_path` are required.

    Returns:
        A `(sql, QueryResult)` tuple. The result carries `GOLD_SQL_UNSAFE` and
        no rows when the reference SQL fails the safety check.
    """
    sql: str = case["gold_sql"]
    if not is_safe_query(sql):
        return sql, QueryResult(columns=[], rows=[], sql=sql, error="GOLD_SQL_UNSAFE")
    return sql, execute_query(case["db_path"], sql)
