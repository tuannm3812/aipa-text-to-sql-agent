"""Spider and BIRD adapters, and the stratified subset draw.

Runs on the hand-written fixtures in tests/fixtures/spider_mini and tests/fixtures/bird_mini,
which use each benchmark's real field names. No network, no real benchmark data.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from text_to_sql_agent.evaluation_v2 import Case, SuiteError, load_suite
from text_to_sql_agent.evaluation_v2.adapters import (
    BIRD_HARDNESS,
    bird_to_cases,
    draw_subset,
    spider_to_cases,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path("tests/fixtures")  # relative to REPO_ROOT, as a real db_root is
SPIDER_ROOT = FIXTURES / "spider_mini" / "database"
BIRD_ROOT = FIXTURES / "bird_mini" / "dev_databases"
SPIDER_HARDNESS = {"0": "easy", "1": "medium", "2": "hard"}


def spider_dev() -> list[dict[str, Any]]:
    return json.loads((REPO_ROOT / FIXTURES / "spider_mini" / "dev.json").read_text())


def bird_dev() -> list[dict[str, Any]]:
    return json.loads((REPO_ROOT / FIXTURES / "bird_mini" / "dev.json").read_text())


def spider_cases() -> list[Case]:
    return spider_to_cases(spider_dev(), db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS)


def bird_cases() -> list[Case]:
    return bird_to_cases(bird_dev(), db_root=BIRD_ROOT)


def round_trip(tmp_path: Path, cases: list[Case]) -> list[Case]:
    path = tmp_path / "suite.jsonl"
    path.write_text("".join(json.dumps(asdict(case)) + "\n" for case in cases))
    return load_suite(path)


# --- Spider ---------------------------------------------------------------------------------


def test_spider_maps_each_record_to_the_contract() -> None:
    dev = spider_dev()
    cases = spider_cases()
    assert [c.id for c in cases] == ["0", "1", "2"]
    for case, record in zip(cases, dev, strict=True):
        assert case.suite == "spider_dev"
        assert case.question == record["question"]
        assert case.gold_sql == record["query"]
        assert case.expected == "answerable"
        assert case.expected_tables == ()
    assert [c.hardness for c in cases] == ["easy", "medium", "hard"]


def test_spider_evidence_is_empty_string() -> None:
    assert all(case.evidence == "" for case in spider_cases())


def test_spider_db_path_is_relative_under_db_root_and_names_the_fixture_file() -> None:
    for case, record in zip(spider_cases(), spider_dev(), strict=True):
        path = Path(case.db_path)
        assert not path.is_absolute()
        assert path.is_relative_to(SPIDER_ROOT)
        assert path == SPIDER_ROOT / record["db_id"] / f"{record['db_id']}.sqlite"
        assert (REPO_ROOT / path).is_file()


def test_spider_cases_load_back_through_load_suite(tmp_path: Path) -> None:
    cases = spider_cases()
    assert round_trip(tmp_path, cases) == cases


def test_spider_record_without_a_hardness_label_is_rejected() -> None:
    with pytest.raises(SuiteError, match="no hardness label"):
        spider_to_cases(spider_dev(), db_root=SPIDER_ROOT, hardness={"0": "easy"})


def test_spider_hardness_label_outside_the_contract_is_rejected() -> None:
    with pytest.raises(SuiteError, match="hardness"):
        spider_to_cases(
            spider_dev(), db_root=SPIDER_ROOT, hardness={**SPIDER_HARDNESS, "1": "trivial"}
        )


# --- BIRD -----------------------------------------------------------------------------------


def test_bird_maps_each_record_to_the_contract() -> None:
    dev = bird_dev()
    cases = bird_cases()
    assert [c.id for c in cases] == ["100", "205", "317"]  # question_id, not list position
    for case, record in zip(cases, dev, strict=True):
        assert case.suite == "bird_dev"
        assert case.question == record["question"]
        assert case.gold_sql == record["SQL"]
        assert case.expected == "answerable"
        assert case.expected_tables == ()


def test_bird_difficulty_maps_to_contract_hardness() -> None:
    assert BIRD_HARDNESS == {"simple": "easy", "moderate": "medium", "challenging": "hard"}
    assert [c.hardness for c in bird_cases()] == ["easy", "medium", "hard"]


def test_bird_evidence_is_carried_and_null_becomes_empty_string() -> None:
    cases = bird_cases()
    assert cases[0].evidence == ""
    assert cases[1].evidence == "taller than 2 metres refers to height_cm > 200"
    assert bird_dev()[2]["evidence"] is None
    assert cases[2].evidence == ""


def test_bird_db_path_is_relative_under_db_root_and_names_the_fixture_file() -> None:
    for case, record in zip(bird_cases(), bird_dev(), strict=True):
        path = Path(case.db_path)
        assert not path.is_absolute()
        assert path == BIRD_ROOT / record["db_id"] / f"{record['db_id']}.sqlite"
        assert (REPO_ROOT / path).is_file()


def test_bird_cases_load_back_through_load_suite(tmp_path: Path) -> None:
    cases = bird_cases()
    assert round_trip(tmp_path, cases) == cases


def test_bird_unknown_difficulty_is_rejected_not_defaulted() -> None:
    dev = bird_dev()
    dev[1]["difficulty"] = "extreme"
    with pytest.raises(SuiteError, match="unknown difficulty 'extreme'"):
        bird_to_cases(dev, db_root=BIRD_ROOT)


def test_bird_duplicate_question_id_is_rejected() -> None:
    dev = bird_dev()
    dev[2]["question_id"] = dev[0]["question_id"]
    with pytest.raises(SuiteError, match="duplicate case id '100'"):
        bird_to_cases(dev, db_root=BIRD_ROOT)


@pytest.mark.parametrize("value", ["7", True, None])
def test_bird_question_id_must_be_an_integer(value: object) -> None:
    dev = bird_dev()
    dev[0]["question_id"] = value
    with pytest.raises(SuiteError, match="question_id"):
        bird_to_cases(dev, db_root=BIRD_ROOT)


# --- Shared source-record validation --------------------------------------------------------


@pytest.mark.parametrize("adapter", ["spider", "bird"])
def test_a_record_missing_db_id_raises_suite_error(adapter: str) -> None:
    if adapter == "spider":
        dev = spider_dev()
        del dev[1]["db_id"]
        with pytest.raises(SuiteError, match="record 1: field 'db_id' is missing"):
            spider_to_cases(dev, db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS)
    else:
        dev = bird_dev()
        del dev[1]["db_id"]
        with pytest.raises(SuiteError, match="record 1: field 'db_id' is missing"):
            bird_to_cases(dev, db_root=BIRD_ROOT)


@pytest.mark.parametrize(
    ("adapter", "field"),
    [("spider", "question"), ("spider", "query"), ("bird", "question"), ("bird", "SQL")],
)
def test_a_blank_required_source_field_raises_suite_error(adapter: str, field: str) -> None:
    dev = spider_dev() if adapter == "spider" else bird_dev()
    dev[0][field] = "   "
    with pytest.raises(SuiteError, match=f"field '{field}' must be a non-empty string"):
        if adapter == "spider":
            spider_to_cases(dev, db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS)
        else:
            bird_to_cases(dev, db_root=BIRD_ROOT)


def test_an_absolute_db_root_is_rejected_so_suite_files_stay_portable() -> None:
    with pytest.raises(SuiteError, match="relative to the repository root"):
        bird_to_cases(bird_dev(), db_root=REPO_ROOT / BIRD_ROOT)


@pytest.mark.parametrize("db_id", ["..", "../etc", "a/b", "a\\b"])
def test_a_db_id_that_would_escape_db_root_is_rejected(db_id: str) -> None:
    dev = bird_dev()
    dev[0]["db_id"] = db_id
    with pytest.raises(SuiteError, match="not a plain directory name"):
        bird_to_cases(dev, db_root=BIRD_ROOT)


def test_an_empty_source_is_rejected() -> None:
    with pytest.raises(SuiteError, match="no records"):
        bird_to_cases([], db_root=BIRD_ROOT)


# --- draw_subset ----------------------------------------------------------------------------


def synthetic(counts: dict[str, Any]) -> list[Case]:
    base = bird_cases()[0]
    cases: list[Case] = []
    for hardness, n in counts.items():
        for _ in range(n):
            cases.append(replace(base, id=str(len(cases)), hardness=hardness))
    return cases


# Spider dev's real hardness counts, so the draw is exercised at the real shape.
SPIDER_SHAPE = {"easy": 248, "medium": 446, "hard": 174, "extra": 166}


def test_draw_subset_is_reproducible_from_its_seed() -> None:
    cases = synthetic(SPIDER_SHAPE)
    assert draw_subset(cases, size=200, seed=20261008) == draw_subset(
        cases, size=200, seed=20261008
    )


def test_draw_subset_depends_on_the_seed() -> None:
    cases = synthetic(SPIDER_SHAPE)
    assert draw_subset(cases, size=200, seed=1) != draw_subset(cases, size=200, seed=2)


def test_draw_subset_returns_exactly_size_unique_ids_from_the_suite_in_suite_order() -> None:
    cases = synthetic(SPIDER_SHAPE)
    ids = draw_subset(cases, size=200, seed=20261008)
    assert len(ids) == 200 == len(set(ids))
    position = {case.id: i for i, case in enumerate(cases)}
    assert all(i in position for i in ids)
    assert ids == sorted(ids, key=position.__getitem__)


@pytest.mark.parametrize(
    "counts",
    [SPIDER_SHAPE, {"easy": 860, "medium": 443, "hard": 231}, {"easy": 3, "hard": 1}],
)
def test_draw_subset_matches_hardness_proportions_within_one_case_per_stratum(
    counts: dict[str, int],
) -> None:
    cases = synthetic(counts)
    size = min(200, len(cases) - 1)
    total = len(cases)
    by_id = {case.id: case.hardness for case in cases}
    drawn = Counter(by_id[i] for i in draw_subset(cases, size=size, seed=20261008))
    for hardness, n in counts.items():
        assert abs(drawn[hardness] - size * n / total) < 1


def test_draw_subset_gives_leftover_slots_to_the_largest_fractional_part() -> None:
    # 10 cases, size 4: exact shares 2.0 / 1.2 / 0.8. Floors 2 / 1 / 0 leave one slot,
    # which goes to hard (fraction .8), not medium (.2) - largest remainder, not largest stratum.
    cases = synthetic({"easy": 5, "medium": 3, "hard": 2})
    by_id = {case.id: case.hardness for case in cases}
    drawn = Counter(by_id[i] for i in draw_subset(cases, size=4, seed=0))
    assert drawn == {"easy": 2, "medium": 1, "hard": 1}


def test_draw_subset_of_the_whole_suite_returns_every_id() -> None:
    cases = synthetic({"easy": 4, "extra": 3})
    assert draw_subset(cases, size=7, seed=5) == [c.id for c in cases]


@pytest.mark.parametrize("size", [0, -1, 8])
def test_draw_subset_rejects_an_impossible_size(size: int) -> None:
    with pytest.raises(ValueError, match="subset size"):
        draw_subset(synthetic({"easy": 4, "extra": 3}), size=size, seed=1)


def test_draw_subset_rejects_duplicate_ids() -> None:
    cases = synthetic({"easy": 3})
    with pytest.raises(ValueError, match="unique"):
        draw_subset([*cases, cases[0]], size=2, seed=1)
