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

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from text_to_sql_agent.evaluation_v2.contract import Case
from text_to_sql_agent.evaluation_v2.identity import IDENTITY_FIELDS
from text_to_sql_agent.evaluation_v2.manifest import Manifest, read_manifest
from text_to_sql_agent.evaluation_v2.report import Row, metric_cells
from text_to_sql_agent.evaluation_v2.runner import (
    CASES_FILE,
    MANIFEST_FILE,
    RunConfig,
    read_rows,
    run_suite,
    select_cases,
)
from text_to_sql_agent.evaluation_v2.scoring import REFUSAL_CODES, UNANSWERABLE
from text_to_sql_agent.evaluation_v2.stats import Interval, paired_bootstrap_ci

_EXPECTED_CODES: dict[str, frozenset[str]] = {
    "expect_refusal": REFUSAL_CODES,
    "expect_unanswerable": frozenset({UNANSWERABLE}),
}


@dataclass(frozen=True)
class GateResult:
    """The gate's verdict and everything it was judged on.

    ``failures`` is empty exactly when ``passed``. ``excepted`` are the ``reference_invalid``
    IDs the exception list covers; ``stale_exceptions`` are listed IDs in the suite that were not
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
    # Set by `regression_gate` only; the gold gate leaves it None.
    regression: RegressionDetail | None = None


def load_exceptions(path: Path | None) -> frozenset[str]:
    """The IDs in an exception list: one per line, ``#`` starts a comment, blanks ignored.

    ``None`` is an empty list. A path that does not exist raises ``OSError``: a gate that
    silently treated a missing list as empty would also pass a typo'd filename. A line that
    holds more than one ID (whitespace or a comma) raises ``ValueError`` naming the line,
    rather than becoming an ID that matches nothing.
    """
    if path is None:
        return frozenset()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{path}: no exception list - create an empty {path.name} to declare no exceptions"
        ) from exc
    ids: set[str] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        if len(entry.split()) != 1 or "," in entry:
            raise ValueError(
                f"{path}:{line_no}: expected one ID per line, got {entry!r} "
                "(put a reason after '#')"
            )
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
    known = {case.id for case in cases}
    failures.extend(
        f"{case_id}: on the exception list but not in the suite (typo or removed case)"
        for case_id in sorted(excepted_ids - known)
    )
    answerable = sum(1 for case in cases if case.expected == "answerable")
    return GateResult(
        suite=name,
        passed=not failures,
        failures=tuple(failures),
        excepted=tuple(sorted(invalid & excepted_ids)),
        stale_exceptions=tuple(sorted((excepted_ids & known) - invalid)),
        answerable=answerable,
        non_answerable=len(cases) - answerable,
        metrics=metric_cells(rows, gold=True),
        run_dir=result.run_dir,
    )


# --- the regression gate (spec §4.5) -------------------------------------------------------

# What may differ between a baseline and a new run: the code under test (`commit`, `dirty`) and
# the prompt. Everything else in the identity payload must agree, so a field added to the
# payload later is compatibility-checked by default rather than silently allowed to differ.
FREE_TO_DIFFER: frozenset[str] = frozenset({"commit", "dirty", "prompt_sha256"})
COMPATIBILITY_FIELDS: tuple[str, ...] = tuple(
    name for name in IDENTITY_FIELDS if name not in FREE_TO_DIFFER
)

RESAMPLES = 10_000
SEED = 0
# A drop of this many percentage points fails regardless of the interval (spec §4.5): it also
# covers the 0/n and n/n cases, where a one-run interval has zero width.
FLOOR_POINTS = 5


class RegressionRefused(ValueError):
    """The two runs cannot be compared (incompatible, incomplete, uncitable, ID mismatch).

    ``differences`` holds ``(field, new value, baseline value)`` for each incompatible field.
    """

    def __init__(self, message: str, differences: tuple[tuple[str, str, str], ...] = ()) -> None:
        """Keep the message and the per-field ``(field, new, baseline)`` differences."""
        super().__init__(message)
        self.differences = differences


@dataclass(frozen=True)
class PairedChange:
    """One population's paired comparison: ``new - old`` on the correct indicator."""

    interval: Interval
    new_correct: int
    old_correct: int


@dataclass(frozen=True)
class RegressionDetail:
    """What the regression verdict rested on.

    ``ex`` is the answerable-case comparison the verdict rests on; ``safety`` is the same rule
    over non-answerable cases, reported but never part of the verdict (``None`` when there are
    none, or both runs are gold runs, which do not score them). ``generation_failures_new`` /
    ``generation_failures_old`` are the IDs whose row is a ``generation_failure`` in that run.
    They stay in the pairing, scored as not correct exactly as headline EX scores them, and are
    listed beside the verdict so a drop caused by failures rather than answers is visible.
    """

    ex: PairedChange
    safety: PairedChange | None
    drop_points: float
    floor_breached: bool
    interval_below_zero: bool
    generation_failures_new: tuple[str, ...]
    generation_failures_old: tuple[str, ...]
    new_commit: str
    old_commit: str
    prompt_changed: bool


def compatible(new: Manifest, old: Manifest) -> list[str]:
    """Names of the identity fields (and ``mode``) on which the two runs disagree.

    Empty when the runs measure the same thing. ``mode`` is checked beside the identity fields:
    a gold run and a model run can share a provider and model label.
    """
    a, b = dataclasses.asdict(new.identity), dataclasses.asdict(old.identity)
    differing = [name for name in COMPATIBILITY_FIELDS if a[name] != b[name]]
    if new.mode != old.mode:
        differing.append("mode")
    return differing


def _field_value(manifest: Manifest, name: str) -> str:
    if name == "mode":
        return manifest.mode
    return str(getattr(manifest.identity, name))


def _load_run(directory: Path, label: str) -> tuple[Manifest, dict[str, Row]]:
    try:
        manifest = Manifest.from_dict(read_manifest(directory / MANIFEST_FILE))
        rows = read_rows(directory / CASES_FILE)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RegressionRefused(f"{label} run {directory}: cannot read it: {exc}") from exc
    if manifest.status != "complete":
        raise RegressionRefused(
            f"{label} run {directory} is {manifest.status}, not complete "
            "(re-run it with --resume first)"
        )
    # A regression verdict is a kind of citation, so the spec's "only a complete, citable run
    # may be cited" applies to both sides.
    if not manifest.citable:
        raise RegressionRefused(
            f"{label} run {directory} is not citable: {manifest.citable_reason}"
        )
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows):
        raise RegressionRefused(f"{label} run {directory}: cases.csv repeats a case id")
    if len(rows) != manifest.case_count:
        raise RegressionRefused(
            f"{label} run {directory}: cases.csv has {len(rows)} rows, "
            f"the manifest says {manifest.case_count}"
        )
    return manifest, by_id


def _paired(ids: list[str], new: dict[str, Row], old: dict[str, Row]) -> PairedChange | None:
    if not ids:
        return None
    new_flags = [new[i]["outcome"] == "correct" for i in ids]
    old_flags = [old[i]["outcome"] == "correct" for i in ids]
    interval = paired_bootstrap_ci(new_flags, old_flags, resamples=RESAMPLES, seed=SEED)
    return PairedChange(interval, sum(new_flags), sum(old_flags))


def regression_gate(new_dir: Path, old_dir: Path) -> GateResult:
    """Compare ``new_dir`` against the baseline ``old_dir`` (spec §4.5); fail on a real drop.

    Cases are aligned by ``id``. The verdict rests on EX over *answerable* cases alone:
    per case ``new - old`` on ``outcome == "correct"``, a paired bootstrap (10,000 resamples,
    fixed seed), and a **fail** when the 95 % interval lies entirely below zero or the point
    drop is 5 percentage points or more. Safety accuracy's change is reported separately.

    The population is every answerable case, and a ``generation_failure`` row counts as not
    correct - exactly as headline EX counts it - so the verdict can never disagree with the
    headline. Dropping such cases from both sides would let a run with 99 failures in 100 be
    compared on the one case that worked and pass. The failures are reported per side beside
    the verdict. A transient provider error is not one of them: it is an ``outage``, which
    leaves the run ``incomplete`` and so refused here until ``--resume`` retries it.

    Raises:
        RegressionRefused: A run is unreadable, incomplete or uncitable, the pair is
            incompatible, the case IDs differ, or no answerable case remains to compare.
    """
    new_manifest, new_rows = _load_run(new_dir, "new")
    old_manifest, old_rows = _load_run(old_dir, "baseline")

    differing = compatible(new_manifest, old_manifest)
    if differing:
        raise RegressionRefused(
            "incompatible runs, differing in: " + ", ".join(differing),
            tuple(
                (name, _field_value(new_manifest, name), _field_value(old_manifest, name))
                for name in differing
            ),
        )
    only_new, only_old = (
        sorted(new_rows.keys() - old_rows.keys()),
        sorted(old_rows.keys() - new_rows.keys()),
    )
    if only_new or only_old:
        raise RegressionRefused(
            f"case IDs differ: {len(only_new)} only in the new run {only_new[:5]}, "
            f"{len(only_old)} only in the baseline {only_old[:5]}"
        )

    ids = sorted(new_rows)
    answerable = [i for i in ids if new_rows[i]["expected"] == "answerable"]
    others = [i for i in ids if new_rows[i]["expected"] != "answerable"]

    ex = _paired(answerable, new_rows, old_rows)
    if ex is None:
        raise RegressionRefused("no answerable case to compare; the verdict rests on EX")
    safety = None if new_manifest.mode == "gold" else _paired(others, new_rows, old_rows)

    # Integer arithmetic: a drop of exactly 5 points must not depend on float rounding.
    net_drop = ex.old_correct - ex.new_correct
    floor_breached = net_drop * 100 >= FLOOR_POINTS * ex.interval.n
    below_zero = ex.interval.high < 0
    failures = []
    if below_zero:
        failures.append(
            f"the 95% interval for the change in EX lies entirely below zero "
            f"[{ex.interval.low:+.1%}, {ex.interval.high:+.1%}]"
        )
    if floor_breached:
        failures.append(
            f"EX dropped {-ex.interval.point:.1%}, at or past the {FLOOR_POINTS}-point floor"
        )

    detail = RegressionDetail(
        ex=ex,
        safety=safety,
        drop_points=-ex.interval.point * 100,
        floor_breached=floor_breached,
        interval_below_zero=below_zero,
        generation_failures_new=tuple(i for i in ids if new_rows[i]["generation_failure"]),
        generation_failures_old=tuple(i for i in ids if old_rows[i]["generation_failure"]),
        new_commit=new_manifest.identity.commit,
        old_commit=old_manifest.identity.commit,
        prompt_changed=new_manifest.identity.prompt_sha256 != old_manifest.identity.prompt_sha256,
    )
    return GateResult(
        suite=new_manifest.identity.suite,
        passed=not failures,
        failures=tuple(failures),
        excepted=(),
        stale_exceptions=(),
        answerable=len(answerable),
        non_answerable=len(others),
        metrics={},
        run_dir=new_dir,
        regression=detail,
    )
