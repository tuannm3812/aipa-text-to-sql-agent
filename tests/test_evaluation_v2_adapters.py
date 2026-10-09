"""Spider and BIRD adapters, and the stratified subset draw.

Runs on the hand-written fixtures in tests/fixtures/spider_mini and tests/fixtures/bird_mini,
which use each benchmark's real field names. No network, no real benchmark data.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from text_to_sql_agent.evaluation_v2 import Case, SuiteError, load_suite
from text_to_sql_agent.evaluation_v2.adapters import (
    BIRD_HARDNESS,
    bird_to_cases,
    draw_subset,
    gold_tables,
    spider_to_cases,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path("tests/fixtures")  # relative to REPO_ROOT, as a real db_root is
SPIDER_ROOT = FIXTURES / "spider_mini" / "database"
BIRD_ROOT = FIXTURES / "bird_mini" / "dev_databases"
SPIDER_HARDNESS = {"0": "easy", "1": "medium", "2": "hard"}


def real_tables(db_root: Path) -> dict[str, list[str]]:
    """Each fixture database's table names, read the way the prepare script reads them."""
    tables: dict[str, list[str]] = {}
    for db in sorted((REPO_ROOT / db_root).iterdir()):
        uri = f"{(db / f'{db.name}.sqlite').as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        tables[db.name] = [name for (name,) in rows]
    return tables


SPIDER_TABLES = real_tables(SPIDER_ROOT)
BIRD_TABLES = real_tables(BIRD_ROOT)


def spider_dev() -> list[dict[str, Any]]:
    return json.loads((REPO_ROOT / FIXTURES / "spider_mini" / "dev.json").read_text())


def bird_dev() -> list[dict[str, Any]]:
    return json.loads((REPO_ROOT / FIXTURES / "bird_mini" / "dev.json").read_text())


def spider_cases() -> list[Case]:
    return spider_to_cases(
        spider_dev(), db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS, tables=SPIDER_TABLES
    )


def bird_cases() -> list[Case]:
    return bird_to_cases(bird_dev(), db_root=BIRD_ROOT, tables=BIRD_TABLES)


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
        spider_to_cases(
            spider_dev(), db_root=SPIDER_ROOT, hardness={"0": "easy"}, tables=SPIDER_TABLES
        )


def test_spider_hardness_label_outside_the_contract_is_rejected() -> None:
    with pytest.raises(SuiteError, match="hardness"):
        spider_to_cases(
            spider_dev(),
            db_root=SPIDER_ROOT,
            hardness={**SPIDER_HARDNESS, "1": "trivial"},
            tables=SPIDER_TABLES,
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
        bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


def test_bird_duplicate_question_id_is_rejected() -> None:
    dev = bird_dev()
    dev[2]["question_id"] = dev[0]["question_id"]
    with pytest.raises(SuiteError, match="duplicate case id '100'"):
        bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


@pytest.mark.parametrize("value", ["7", True, None])
def test_bird_question_id_must_be_an_integer(value: object) -> None:
    dev = bird_dev()
    dev[0]["question_id"] = value
    with pytest.raises(SuiteError, match="question_id"):
        bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


# --- expected_tables -----------------------------------------------------------------------


def test_spider_expected_tables_come_from_the_gold_sql_including_subqueries() -> None:
    # Record 2 reads employee only inside a NOT IN subquery; it still counts.
    assert [c.expected_tables for c in spider_cases()] == [
        ("pets",),
        ("employee",),
        ("employee", "shop"),
    ]


def test_bird_expected_tables_come_from_the_gold_sql() -> None:
    assert [c.expected_tables for c in bird_cases()] == [
        ("member",),
        ("superhero",),
        ("expense", "member"),
    ]


def bird_with_gold(sql: str) -> Case:
    dev = bird_dev()
    dev[0]["SQL"] = sql
    return bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)[0]


def test_a_cte_name_is_not_an_expected_table() -> None:
    case = bird_with_gold(
        "WITH big_spenders AS (SELECT member_id FROM expense WHERE cost > 10) "
        "SELECT COUNT(*) FROM big_spenders JOIN member USING (member_id)"
    )
    assert case.expected_tables == ("expense", "member")


def test_a_self_join_yields_one_table() -> None:
    case = bird_with_gold(
        "SELECT a.name FROM member AS a JOIN member AS b ON a.member_id < b.member_id"
    )
    assert case.expected_tables == ("member",)


def test_expected_tables_are_lowercased_deduplicated_and_sorted() -> None:
    case = bird_with_gold(
        "SELECT COUNT(*) FROM Member JOIN EXPENSE ON EXPENSE.member_id = Member.member_id "
        "WHERE Member.member_id IN (SELECT member_id FROM member)"
    )
    assert case.expected_tables == ("expense", "member")


def test_a_gold_query_reading_no_table_has_no_expected_tables() -> None:
    assert bird_with_gold("SELECT 1").expected_tables == ()


def test_a_table_the_database_does_not_have_is_a_conversion_error_naming_the_case() -> None:
    with pytest.raises(SuiteError, match=r"bird_dev case 100: gold SQL reads \['members'\]"):
        bird_with_gold("SELECT COUNT(*) FROM members")


def test_a_table_from_another_database_is_a_conversion_error() -> None:
    # superhero exists, but in hero_mini, not in this case's club_mini.
    with pytest.raises(SuiteError, match="not tables of database 'club_mini'"):
        bird_with_gold("SELECT COUNT(*) FROM superhero")


def test_an_unparseable_gold_query_is_a_conversion_error_naming_the_case() -> None:
    with pytest.raises(SuiteError, match="bird_dev case 100: gold SQL does not parse"):
        bird_with_gold("SELECT FROM WHERE (")


def test_a_multi_statement_gold_query_is_a_conversion_error() -> None:
    with pytest.raises(SuiteError, match="must be one statement"):
        bird_with_gold("SELECT 1 FROM member; SELECT 2 FROM member")


def test_a_database_missing_from_the_table_map_is_a_conversion_error() -> None:
    tables = {k: v for k, v in BIRD_TABLES.items() if k != "hero_mini"}
    with pytest.raises(SuiteError, match="case 205: no table list for database 'hero_mini'"):
        bird_to_cases(bird_dev(), db_root=BIRD_ROOT, tables=tables)


def test_gold_tables_on_its_own() -> None:
    assert gold_tables("WITH t AS (SELECT 1) SELECT * FROM t") == []
    assert gold_tables("SELECT * FROM `Card Games` AS c") == ["card games"]


def test_a_nested_cte_does_not_hide_an_outer_real_table_of_the_same_name() -> None:
    # Codex's fixture: the inner `orders` CTE is visible only inside its own subquery, so the
    # outer `orders` is the physical table and must be in the expected set.
    sql = (
        "SELECT id FROM orders WHERE id IN ("
        "WITH orders AS (SELECT id FROM customers) SELECT id FROM orders)"
    )
    assert gold_tables(sql) == ["customers", "orders"]


def test_a_table_read_inside_and_outside_a_cte_appears_once() -> None:
    sql = (
        "WITH big AS (SELECT id FROM orders WHERE total > 10) "
        "SELECT o.id FROM orders AS o JOIN big ON big.id = o.id"
    )
    assert gold_tables(sql) == ["orders"]


def _sqlite_answer(sql: str) -> object:
    """What SQLite itself does with ``sql`` over tables ``t`` and ``customers``."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("CREATE TABLE customers (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (7)")
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            return str(exc)


def test_a_cte_body_naming_its_own_cte_binds_to_the_cte_in_sqlite() -> None:
    # SQLite does not read the real `customers` here: inside the body the name binds to the CTE
    # itself, and SQLite rejects the query as a circular reference. So no such query can be a
    # valid gold reference, and the name is a CTE reference, not a table read.
    sql = "WITH customers AS (SELECT * FROM customers) SELECT * FROM customers"
    assert _sqlite_answer(sql) == "circular reference: customers"
    assert gold_tables(sql) == []


def test_a_forward_referenced_sibling_cte_is_the_cte_in_sqlite() -> None:
    # SQLite resolves `b` in the first CTE's body to the *later* sibling CTE `b`.
    sql = "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x FROM t) SELECT * FROM a"
    assert _sqlite_answer(sql) == [(1,)]
    assert gold_tables(sql) == ["t"]


def test_sibling_cte_names_do_not_reach_outside_their_with() -> None:
    sql = (
        "SELECT * FROM b WHERE x IN ("
        "WITH a AS (SELECT * FROM b), b AS (SELECT 1 AS x FROM t) SELECT x FROM a)"
    )
    assert gold_tables(sql) == ["b", "t"]


def test_a_recursive_ctes_self_reference_is_not_a_table() -> None:
    sql = (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) "
        "SELECT n FROM r"
    )
    assert gold_tables(sql) == []


# --- Shared source-record validation --------------------------------------------------------


@pytest.mark.parametrize("adapter", ["spider", "bird"])
def test_a_record_missing_db_id_raises_suite_error(adapter: str) -> None:
    if adapter == "spider":
        dev = spider_dev()
        del dev[1]["db_id"]
        with pytest.raises(SuiteError, match="record 1: field 'db_id' is missing"):
            spider_to_cases(
                dev, db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS, tables=SPIDER_TABLES
            )
    else:
        dev = bird_dev()
        del dev[1]["db_id"]
        with pytest.raises(SuiteError, match="record 1: field 'db_id' is missing"):
            bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


@pytest.mark.parametrize(
    ("adapter", "field"),
    [("spider", "question"), ("spider", "query"), ("bird", "question"), ("bird", "SQL")],
)
def test_a_blank_required_source_field_raises_suite_error(adapter: str, field: str) -> None:
    dev = spider_dev() if adapter == "spider" else bird_dev()
    dev[0][field] = "   "
    with pytest.raises(SuiteError, match=f"field '{field}' must be a non-empty string"):
        if adapter == "spider":
            spider_to_cases(
                dev, db_root=SPIDER_ROOT, hardness=SPIDER_HARDNESS, tables=SPIDER_TABLES
            )
        else:
            bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


def test_an_absolute_db_root_is_rejected_so_suite_files_stay_portable() -> None:
    with pytest.raises(SuiteError, match="relative to the repository root"):
        bird_to_cases(bird_dev(), db_root=REPO_ROOT / BIRD_ROOT, tables=BIRD_TABLES)


@pytest.mark.parametrize("db_id", ["..", "../etc", "a/b", "a\\b"])
def test_a_db_id_that_would_escape_db_root_is_rejected(db_id: str) -> None:
    dev = bird_dev()
    dev[0]["db_id"] = db_id
    with pytest.raises(SuiteError, match="not a plain directory name"):
        bird_to_cases(dev, db_root=BIRD_ROOT, tables=BIRD_TABLES)


def test_an_empty_source_is_rejected() -> None:
    with pytest.raises(SuiteError, match="no records"):
        bird_to_cases([], db_root=BIRD_ROOT, tables=BIRD_TABLES)


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
