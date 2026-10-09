"""Convert Spider 1.0 dev and BIRD dev into the v2 case contract, and draw subsets.

Pure functions on already-parsed JSON. Downloading, hashing and unpacking live in
``scripts/prepare_benchmarks.py``; nothing here touches the network or the filesystem.

``db_path`` is ``<db_root>/<db_id>/<db_id>.sqlite`` with ``db_root`` relative to the repository
root (for example ``data/benchmarks/spider/database``), so a generated suite file names the
same database on every machine. ``load_suite`` does not check that the file exists, by
design: a suite can be validated, hashed and subset without the data present.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, get_args

from text_to_sql_agent.evaluation_v2.contract import Case, Hardness, SuiteError, _parse_record

# Bump when a change here would alter any generated record; the run manifest records it via
# each suite's .source.json, so results from different conversions are never compared blind.
ADAPTER_VERSION = "1"

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


def _db_path(suite: str, db_root: Path, db_id: str) -> str:
    if db_root.is_absolute():
        raise SuiteError(f"{suite}: db_root must be relative to the repository root: {db_root}")
    # Reject anything that would let a db_id escape db_root ("../x", "a/b").
    if db_id in {".", ".."} or "/" in db_id or "\\" in db_id:
        raise SuiteError(f"{suite}: db_id '{db_id}' is not a plain directory name")
    return str(PurePosixPath(db_root.as_posix()) / db_id / f"{db_id}.sqlite")


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
    dev: list[dict[str, Any]], *, db_root: Path, hardness: Mapping[str, str]
) -> list[Case]:
    """Spider ``dev.json`` records as cases.

    ``id`` is the record's 0-based position in ``dev.json`` (Spider has no question ID of its
    own, and every Spider tool indexes dev this way). ``hardness`` maps that ID to the official
    label, precomputed with ``spider_hardness.spider_hardness``. Spider has no evidence, so
    ``evidence`` is ``""``. ``expected_tables`` is left empty: deriving it needs the database's
    own table-name spelling, which a pure adapter does not have.
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
                    "db_path": _db_path(SPIDER_SUITE, db_root, db_id),
                    "question": question,
                    "evidence": "",
                    "gold_sql": query,
                    "hardness": hardness[case_id],
                    "expected": "answerable",
                    "expected_tables": [],
                },
            )
        )
    return _check_unique(SPIDER_SUITE, cases)


def bird_to_cases(dev: list[dict[str, Any]], *, db_root: Path) -> list[Case]:
    """BIRD dev records as cases.

    ``id`` is BIRD's own ``question_id``. ``evidence`` is carried verbatim (``null`` becomes
    ``""``; the 2025-11-06 dataset card allows either). ``difficulty`` maps through
    ``BIRD_HARDNESS``; an unknown value is an error, not a default.
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
        cases.append(
            _to_case(
                BIRD_SUITE,
                position,
                {
                    "suite": BIRD_SUITE,
                    "id": str(question_id),
                    "db_path": _db_path(BIRD_SUITE, db_root, db_id),
                    "question": question,
                    "evidence": evidence,
                    "gold_sql": sql,
                    "hardness": BIRD_HARDNESS[difficulty],
                    "expected": "answerable",
                    "expected_tables": [],
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
