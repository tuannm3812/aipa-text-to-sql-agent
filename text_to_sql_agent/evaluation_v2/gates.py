"""The gold gate (spec §4.4, §4.5): a judgement over a gold run, not a second run loop.

``gold_gate`` runs ``run_suite`` in gold mode - no model, no provider, no network - and passes
when all three hold:

1. every *valid* reference executes and self-matches under ``rows_equal_v2`` (a gold row that
   is neither ``correct`` nor ``reference_invalid`` is a comparator that disagrees with itself);
2. every ``reference_invalid`` ID is on the suite's reviewed exception list
   (``evaluation/suites/<suite>.gold_exceptions.txt``);
3. every non-answerable case is well-formed: no gold SQL, and an ``expected`` whose correct
   outcome the pipeline can actually produce.

Headline EX and reference coverage are reported either way, so an excepted reference lowers
the headline (nine valid plus one excepted is a passing gate at 9/10) without failing the gate.
Two populations are kept apart: a suite whose answerable cases all have invalid references
reports headline ``0/N`` and conditional ``0/0 (undefined)``; a suite with no answerable cases
at all (``safety``) reports EX *not applicable* and rests on check 3 alone. Safety accuracy is a
model metric, so the gate never reports it (``metric_cells(..., gold=True)``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from text_to_sql_agent.evaluation_v2.contract import Case
from text_to_sql_agent.evaluation_v2.report import Row, metric_cells
from text_to_sql_agent.evaluation_v2.runner import RunConfig, run_suite, select_cases
from text_to_sql_agent.evaluation_v2.scoring import REFUSAL_CODES, UNANSWERABLE

_EXPECTED_CODES: dict[str, frozenset[str]] = {
    "expect_refusal": REFUSAL_CODES,
    "expect_unanswerable": frozenset({UNANSWERABLE}),
}


@dataclass(frozen=True)
class GateResult:
    """The gate's verdict and everything it was judged on.

    ``failures`` is empty exactly when ``passed``. ``excepted`` are the ``reference_invalid``
    IDs the exception list covers; ``stale_exceptions`` are listed IDs that were not
    ``reference_invalid`` this run (informational: a fixed reference leaves a stale entry, and
    the list should then be pruned). ``metrics`` are the gold-mode report cells for the whole
    suite, keyed as ``report.COLUMNS``.
    """

    suite: str
    passed: bool
    failures: tuple[str, ...]
    excepted: tuple[str, ...]
    stale_exceptions: tuple[str, ...]
    answerable: int
    non_answerable: int
    metrics: dict[str, str]
    run_dir: Path


def load_exceptions(path: Path | None) -> frozenset[str]:
    """The IDs in an exception list: one per line, ``#`` starts a comment, blanks ignored.

    ``None`` is an empty list. A path that does not exist raises ``OSError``: a gate that
    silently treated a missing list as empty would also pass a typo'd filename.
    """
    if path is None:
        return frozenset()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            ids.add(entry)
    return frozenset(ids)


def _malformed(cases: list[Case]) -> list[str]:
    problems = []
    for case in cases:
        if case.expected == "answerable":
            continue
        if case.gold_sql != "":
            problems.append(f"{case.id}: non-answerable case carries gold SQL")
        if not _EXPECTED_CODES.get(case.expected):
            problems.append(f"{case.id}: no code the pipeline can produce for '{case.expected}'")
    return problems


def judge(cases: list[Case], rows: list[Row], excepted_ids: frozenset[str]) -> list[str]:
    """The gate's failures over a gold run's rows; empty when it passes."""
    failures = _malformed(cases)
    for row in rows:
        if row["expected"] != "answerable":
            continue
        if row["outcome"] == "reference_invalid":
            if row["id"] not in excepted_ids:
                failures.append(
                    f"{row['id']}: reference_invalid and not on the exception list "
                    f"({row['error'] or 'no error text'})"
                )
        elif row["outcome"] != "correct":
            failures.append(
                f"{row['id']}: a valid reference scored '{row['outcome']}' against itself"
            )
    return failures


def gold_gate(
    suite_path: Path,
    exceptions_path: Path | None,
    *,
    out_root: Path,
    work_limit: int | None = None,
    max_rows: int | None = None,
) -> GateResult:
    """Run ``suite_path`` in gold mode under ``out_root`` and judge it against the exceptions.

    ``out_root`` is required and keyword-only: the gate must never default into
    ``evaluation/results/``. Public suites need an explicit ``work_limit`` and ``max_rows``
    (the runner refuses otherwise).

    Raises:
        OSError: If ``exceptions_path`` is given but unreadable.
        SuiteError, ValueError, ResumeRefused: As ``select_cases`` and ``run_suite``.
    """
    excepted_ids = load_exceptions(exceptions_path)
    name = suite_path.name.removesuffix(".jsonl")
    config = RunConfig(
        suite=name,
        suite_path=suite_path,
        mode="gold",
        provider="gold",
        model="gold",
        work_limit=work_limit,
        max_rows=max_rows,
    )
    cases = select_cases(config)
    result = run_suite(cases, config=config, out_root=out_root)
    rows = result.rows
    invalid = {row["id"] for row in rows if row["outcome"] == "reference_invalid"}
    failures = judge(cases, rows, excepted_ids)
    answerable = sum(1 for case in cases if case.expected == "answerable")
    return GateResult(
        suite=name,
        passed=not failures,
        failures=tuple(failures),
        excepted=tuple(sorted(invalid & excepted_ids)),
        stale_exceptions=tuple(sorted(excepted_ids - invalid)),
        answerable=answerable,
        non_answerable=len(cases) - answerable,
        metrics=metric_cells(rows, gold=True),
        run_dir=result.run_dir,
    )
