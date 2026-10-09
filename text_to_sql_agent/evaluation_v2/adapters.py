"""Convert Spider 1.0 dev and BIRD dev into the v2 case contract, and draw subsets.

Pure functions on already-parsed JSON. Downloading, hashing, unpacking and opening the
databases live in ``scripts/prepare_benchmarks.py``; nothing here touches the network or the
filesystem. What the adapters need from the databases - each one's real table names - is
passed in as data.

``db_path`` is ``<db_root>/<db_id>/<db_id>.sqlite`` with ``db_root`` relative to the repository
root (for example ``data/benchmarks/spider/database``), so a generated suite file names the
same database on every machine. ``load_suite`` does not check that the file exists, by
design: a suite can be validated, hashed and subset without the data present.

``expected_tables`` is derived from the gold SQL: every base table the query reads, CTE
references excluded scope by scope, lowercased, deduplicated and sorted, and each one checked
against the database's real table names. A gold query that reads a table the database does
not have, or that does not parse, is a conversion error naming the case - never a silently
shorter list. Names are lowercased because SQLite table names are case-insensitive and
Spider's gold SQL spells them freely (``CAR_MAKERS`` for ``car_makers``); whoever compares
them must lowercase too.
"""

from __future__ import annotations

import random
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, get_args

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from text_to_sql_agent.evaluation_v2.contract import Case, Hardness, SuiteError, _parse_record
from text_to_sql_agent.safety import _names_visible_cte

# Bump when a change here would alter any generated record; the run manifest records it via
# each suite's .source.json, so results from different conversions are never compared blind.
# 2: expected_tables derived from the gold SQL (was always empty in 1).
# 3: CTE names resolved per scope, by SQLite's rules: a nested CTE no longer hides an outer
#    real table that shares its name, and a CTE body sees every sibling in its WITH list.
ADAPTER_VERSION = "3"

SPIDER_SUITE = "spider_dev"
BIRD_SUITE = "bird_dev"

# BIRD's three levels onto the contract's first three; BIRD has no "extra".
BIRD_HARDNESS: dict[str, str] = {"simple": "easy", "moderate": "medium", "challenging": "hard"}


def _require_str(suite: str, position: int, record: Any, field: str) -> str:
    if not isinstance(record, dict):
        raise SuiteError(f"{suite} source record {position}: must be a JSON object")
    if field not in record:
        raise SuiteError(f"{suite} source record {position}: field '{field}' is missing")
    value = record[field]
    if not isinstance(value, str) or not value.strip():
        raise SuiteError(
            f"{suite} source record {position}: field '{field}' must be a non-empty string"
        )
    return value


def database_path(db_root: Path, db_id: str, *, suite: str) -> str:
    """The repo-relative ``db_path`` for ``db_id``; rejects anything that is not portable."""
    if db_root.is_absolute():
        raise SuiteError(f"{suite}: db_root must be relative to the repository root: {db_root}")
    # Reject anything that would let a db_id escape db_root ("../x", "a/b").
    if db_id in {".", ".."} or "/" in db_id or "\\" in db_id:
        raise SuiteError(f"{suite}: db_id '{db_id}' is not a plain directory name")
    return str(PurePosixPath(db_root.as_posix()) / db_id / f"{db_id}.sqlite")


def gold_tables(sql: str) -> list[str]:
    """Base tables ``sql`` reads: CTE references excluded, lowercased, deduplicated, sorted.

    Whether a reference names a CTE or a real table is decided at that reference's own
    position (``_names_sqlite_cte``), never by a global set of CTE names: a nested CTE must not
    hide an outer real table of the same name.

    Raises ``ValueError`` when ``sql`` is not exactly one parseable statement.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as exc:
        raise ValueError(f"gold SQL does not parse: {exc}") from exc
    if len(statements) != 1:
        raise ValueError(f"gold SQL must be one statement, found {len(statements)}")
    names = {
        str(table.name).lower()
        for table in statements[0].find_all(exp.Table)
        if not _names_sqlite_cte(table)
    }
    return sorted(name for name in names if name)


def _names_sqlite_cte(table: exp.Table) -> bool:
    """Whether bare ``table`` binds to a CTE at its position, by SQLite's own rules.

    ``safety._names_visible_cte`` is the shared scope walk, so it is asked first: it decides
    the main-query and nesting rules, and a CTE is never visible outside the query owning its
    ``WITH``. Its sibling rule, though, is DuckDB's (only *earlier* siblings, plus the CTE itself
    under ``RECURSIVE``), which SQLite does not follow: in SQLite every name in a ``WITH`` list
    is visible inside every CTE body of that list - a later sibling (``WITH a AS (SELECT *
    FROM b), b AS (...)`` reads the CTE ``b``) and the CTE's own name (which SQLite then
    rejects as a circular reference unless it is a valid recursive CTE). That widening is
    applied here, for gold-table extraction only; the validator's visibility rules, which are
    security-relevant, are unchanged.
    """
    if _names_visible_cte(table, dialect="sqlite"):
        return True
    if table.args.get("db") or table.args.get("catalog"):
        return False
    name = str(table.name).lower()
    node: exp.Expression = table
    while node.parent is not None:
        parent = node.parent
        if (
            isinstance(parent, exp.CTE)
            and isinstance(parent.parent, exp.With)
            and any(str(cte.alias_or_name).lower() == name for cte in parent.parent.expressions)
        ):
            return True
        node = parent
    return False


def _expected_tables(
    suite: str, case_id: str, db_id: str, sql: str, tables: Mapping[str, Collection[str]]
) -> list[str]:
    if db_id not in tables:
        raise SuiteError(f"{suite} case {case_id}: no table list for database '{db_id}'")
    try:
        read = gold_tables(sql)
    except ValueError as exc:
        raise SuiteError(f"{suite} case {case_id}: {exc}") from exc
    real = {name.lower() for name in tables[db_id]}
    missing = [name for name in read if name not in real]
    if missing:
        raise SuiteError(
            f"{suite} case {case_id}: gold SQL reads {missing}, not tables of database '{db_id}'"
        )
    return read


def _to_case(suite: str, position: int, record: dict[str, Any]) -> Case:
    # The same validation load_suite applies, so a conversion that would not load fails here.
    return _parse_record(Path(f"<{suite} adapter>"), position, record)


def _check_unique(suite: str, cases: list[Case]) -> list[Case]:
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise SuiteError(f"{suite}: duplicate case id '{case.id}'")
        seen.add(case.id)
    if not cases:
        raise SuiteError(f"{suite}: source contains no records")
    return cases


def spider_to_cases(
    dev: list[dict[str, Any]],
    *,
    db_root: Path,
    hardness: Mapping[str, str],
    tables: Mapping[str, Collection[str]],
) -> list[Case]:
    """Spider ``dev.json`` records as cases.

    ``id`` is the record's 0-based position in ``dev.json`` (Spider has no question ID of its
    own, and every Spider tool indexes dev this way). ``hardness`` maps that ID to the official
    label, precomputed with ``spider_hardness.spider_hardness``. ``tables`` maps each ``db_id``
    to its database's real table names, for ``expected_tables``. Spider has no evidence, so
    ``evidence`` is ``""``.
    """
    cases: list[Case] = []
    for position, record in enumerate(dev):
        db_id = _require_str(SPIDER_SUITE, position, record, "db_id")
        question = _require_str(SPIDER_SUITE, position, record, "question")
        query = _require_str(SPIDER_SUITE, position, record, "query")
        case_id = str(position)
        if case_id not in hardness:
            raise SuiteError(f"{SPIDER_SUITE} source record {position}: no hardness label")
        cases.append(
            _to_case(
                SPIDER_SUITE,
                position,
                {
                    "suite": SPIDER_SUITE,
                    "id": case_id,
                    "db_path": database_path(db_root, db_id, suite=SPIDER_SUITE),
                    "question": question,
                    "evidence": "",
                    "gold_sql": query,
                    "hardness": hardness[case_id],
                    "expected": "answerable",
                    "expected_tables": _expected_tables(
                        SPIDER_SUITE, case_id, db_id, query, tables
                    ),
                },
            )
        )
    return _check_unique(SPIDER_SUITE, cases)


def bird_to_cases(
    dev: list[dict[str, Any]], *, db_root: Path, tables: Mapping[str, Collection[str]]
) -> list[Case]:
    """BIRD dev records as cases.

    ``id`` is BIRD's own ``question_id``. ``evidence`` is carried verbatim (``null`` becomes
    ``""``; the 2025-11-06 dataset card allows either). ``difficulty`` maps through
    ``BIRD_HARDNESS``; an unknown value is an error, not a default. ``tables`` maps each
    ``db_id`` to its database's real table names, for ``expected_tables``.
    """
    cases: list[Case] = []
    for position, record in enumerate(dev):
        db_id = _require_str(BIRD_SUITE, position, record, "db_id")
        question = _require_str(BIRD_SUITE, position, record, "question")
        sql = _require_str(BIRD_SUITE, position, record, "SQL")
        difficulty = _require_str(BIRD_SUITE, position, record, "difficulty")
        question_id = record.get("question_id")
        # bool is an int subclass; True is not a question ID.
        if not isinstance(question_id, int) or isinstance(question_id, bool):
            raise SuiteError(
                f"{BIRD_SUITE} source record {position}: field 'question_id' must be an integer"
            )
        if difficulty not in BIRD_HARDNESS:
            raise SuiteError(
                f"{BIRD_SUITE} source record {position}: unknown difficulty '{difficulty}'"
            )
        evidence = record.get("evidence")
        if evidence is None:
            evidence = ""
        if not isinstance(evidence, str):
            raise SuiteError(
                f"{BIRD_SUITE} source record {position}: field 'evidence' must be a string"
            )
        case_id = str(question_id)
        cases.append(
            _to_case(
                BIRD_SUITE,
                position,
                {
                    "suite": BIRD_SUITE,
                    "id": case_id,
                    "db_path": database_path(db_root, db_id, suite=BIRD_SUITE),
                    "question": question,
                    "evidence": evidence,
                    "gold_sql": sql,
                    "hardness": BIRD_HARDNESS[difficulty],
                    "expected": "answerable",
                    "expected_tables": _expected_tables(BIRD_SUITE, case_id, db_id, sql, tables),
                },
            )
        )
    return _check_unique(BIRD_SUITE, cases)


def draw_subset(cases: Sequence[Case], *, size: int, seed: int) -> list[str]:
    """A hardness-stratified sample of ``size`` case IDs, deterministic in ``seed``.

    Each stratum gets ``floor(size * n_h / N)`` cases; the remaining slots go one each to the
    strata with the largest fractional part (ties: contract order easy, medium, hard, extra).
    So every stratum is within one case of its exact proportional share. Strata are sampled
    in contract order from one ``random.Random(seed)``. IDs are returned in suite order.
    """
    total = len(cases)
    if not 0 < size <= total:
        raise ValueError(f"subset size must be between 1 and {total}, got {size}")
    order = {case.id: index for index, case in enumerate(cases)}
    if len(order) != total:
        raise ValueError("case IDs must be unique to draw a subset")

    strata: dict[str, list[str]] = {}
    for case in cases:
        strata.setdefault(case.hardness, []).append(case.id)
    present = [level for level in get_args(Hardness) if level in strata]

    # Integer arithmetic throughout, so no float rounding can reorder a tie.
    quotas = {level: size * len(strata[level]) // total for level in present}
    remainders = {level: size * len(strata[level]) % total for level in present}
    by_fraction = sorted(present, key=lambda level: (-remainders[level], present.index(level)))
    for level in by_fraction[: size - sum(quotas.values())]:
        quotas[level] += 1

    rng = random.Random(seed)
    chosen: list[str] = []
    for level in present:
        chosen.extend(rng.sample(strata[level], quotas[level]))
    return sorted(chosen, key=order.__getitem__)
