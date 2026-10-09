"""The vendored Spider hardness classifier reproduces the official labels.

`tests/fixtures/spider_mini/hardness_reference.json` holds six records copied verbatim from
Spider 1.0 `dev.json` (CC BY-SA 4.0, Yu et al. 2018) - index, db_id, query and the parsed
`sql` - each with the label the unmodified official `evaluation.py` (taoyds/spider @ b7b5b8c)
assigned it on 2026-10-09. At least one per class; `hard` and `extra` each appear twice so
both the component1 route and the nested/set-operation (component2) route are covered.
Each label was also checked by hand against the classifier's rules; the reasoning is the
comment beside each expected count below.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from text_to_sql_agent.evaluation_v2.spider_hardness import spider_hardness

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "spider_mini"
REFERENCE: list[dict[str, Any]] = json.loads((FIXTURES / "hardness_reference.json").read_text())

# dev index -> why the official script says what it says (comp1 / comp2 / others).
HAND_CHECK = {
    295: "easy: no clause components, one aggregate, one select column",
    506: "medium: WHERE + LIKE make comp1 = 2, others = 0",
    331: "hard: GROUP BY + ORDER BY + LIMIT make comp1 = 3",
    974: "hard: comp1 = 1 (WHERE), one nested query (comp2 = 1), others = 0",
    107: "extra: JOIN + GROUP BY + ORDER BY + LIMIT make comp1 = 4",
    505: "extra: EXCEPT (comp2 = 1) with three select columns (others = 1)",
}


def test_reference_covers_every_class() -> None:
    assert {r["official_hardness"] for r in REFERENCE} == {"easy", "medium", "hard", "extra"}
    assert {r["dev_index"] for r in REFERENCE} == set(HAND_CHECK)


@pytest.mark.parametrize("record", REFERENCE, ids=lambda r: f"dev{r['dev_index']}")
def test_vendored_classifier_matches_the_official_label(record: dict[str, Any]) -> None:
    assert HAND_CHECK[record["dev_index"]].startswith(record["official_hardness"] + ":")
    assert spider_hardness(record["sql"]) == record["official_hardness"]


def test_mini_fixture_labels() -> None:
    dev = json.loads((FIXTURES / "dev.json").read_text())
    assert [spider_hardness(r["sql"]) for r in dev] == ["easy", "medium", "hard"]
