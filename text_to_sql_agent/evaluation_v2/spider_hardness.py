"""Spider 1.0's official hardness classifier, vendored.

Spider's ``dev.json`` carries no hardness label. The ``easy/medium/hard/extra`` split every
Spider paper reports is computed by the official evaluation script, so this module reproduces
exactly that computation and nothing else.

Provenance
----------
Vendored from ``evaluation.py`` in https://github.com/taoyds/spider at commit
``b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c`` (master, 2024-05-29; the file itself was last
changed in ``cccfe7bc99c4f5b7229890bbff75bfed50f2b008``, 2020-05-27). Copyright the Spider
authors, licensed Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0); this repository
is MIT. Taken: ``WHERE_OPS``, ``UNIT_OPS`` (index of ``'none'`` only), ``AGG_OPS`` (likewise),
``has_agg``, ``count_agg``, ``get_nestedSQL``, ``count_component1``, ``count_component2``,
``count_others`` and ``Evaluator.eval_hardness``. Not taken: the SQL parser
(``process_sql.py``), the exact-match and execution evaluators, and everything else.

Changes from the original, none of which alters a result: type annotations; snake_case names
(``get_nestedSQL`` -> ``_nested_sql``); ``Evaluator.eval_hardness`` became the module-level
``spider_hardness``; ``type(x) is dict`` became ``isinstance(x, dict)`` (the input is parsed
JSON, so no ``dict`` subclass can appear); list-comprehension ``len(...)`` counts became
``sum(...)``.

Why no parser
-------------
The official script re-parses each gold query with ``process_sql.get_sql`` before classifying
it. ``dev.json`` already ships that parse in each record's ``sql`` field (its README: "used
process_sql.py to reparse SQL queries", 2020-08-03), and hardness reads only the parse's
structure, never column or table IDs. Checked on 2026-10-09: the unmodified official
``eval_hardness(get_sql(...))`` over ``dev_gold.sql`` and this function over ``dev.json``'s
``sql`` fields agree on all 1,034 dev questions (248 easy, 446 medium, 174 hard, 166 extra,
which is the published split).

Quirks kept on purpose
----------------------
``count_others`` passes WHERE condition units and the raw HAVING list (``'and'``/``'or'``
strings included) to ``count_agg``, which tests element 0 against the ``'none'`` aggregate
index. For a condition unit element 0 is the NOT flag; for ``'and'`` it is the letter ``'a'``.
Both are bugs in the original and both are reproduced, because the point is the official
label, not a better one.
"""

from __future__ import annotations

from typing import Any

from text_to_sql_agent.evaluation_v2.contract import Hardness

# Index positions from the original's tuples; only these three are ever consulted.
_WHERE_OP_LIKE = 9  # WHERE_OPS.index('like')
_UNIT_OP_NONE = 0  # UNIT_OPS.index('none')
_AGG_NONE = 0  # AGG_OPS.index('none')

# Parsed-SQL shape, from the original's header comment:
#   col_unit:   (agg_id, col_id, isDistinct)
#   val_unit:   (unit_op, col_unit1, col_unit2)
#   cond_unit:  (not_op, op_id, val_unit, val1, val2)
#   condition:  [cond_unit1, 'and'/'or', cond_unit2, ...]
#   sql: {'select': (isDistinct, [(agg_id, val_unit), ...]),
#         'from': {'table_units': [...], 'conds': condition}, 'where': condition,
#         'groupBy': [col_unit, ...], 'orderBy': ('asc'/'desc', [val_unit, ...]),
#         'having': condition, 'limit': None | int,
#         'intersect': None | sql, 'except': None | sql, 'union': None | sql}
ParsedSql = dict[str, Any]


def _has_agg(unit: Any) -> bool:
    return bool(unit[0] != _AGG_NONE)


def _count_agg(units: list[Any]) -> int:
    return sum(1 for unit in units if _has_agg(unit))


def _nested_sql(sql: ParsedSql) -> list[ParsedSql]:
    nested: list[ParsedSql] = []
    for cond_unit in sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]:
        if isinstance(cond_unit[3], dict):
            nested.append(cond_unit[3])
        if isinstance(cond_unit[4], dict):
            nested.append(cond_unit[4])
    for op in ("intersect", "except", "union"):
        if sql[op] is not None:
            nested.append(sql[op])
    return nested


def _count_component1(sql: ParsedSql) -> int:
    count = 0
    if len(sql["where"]) > 0:
        count += 1
    if len(sql["groupBy"]) > 0:
        count += 1
    if len(sql["orderBy"]) > 0:
        count += 1
    if sql["limit"] is not None:
        count += 1
    if len(sql["from"]["table_units"]) > 0:  # JOIN
        count += len(sql["from"]["table_units"]) - 1

    and_or = sql["from"]["conds"][1::2] + sql["where"][1::2] + sql["having"][1::2]
    count += sum(1 for token in and_or if token == "or")
    cond_units = sql["from"]["conds"][::2] + sql["where"][::2] + sql["having"][::2]
    count += sum(1 for cond_unit in cond_units if cond_unit[1] == _WHERE_OP_LIKE)
    return count


def _count_component2(sql: ParsedSql) -> int:
    return len(_nested_sql(sql))


def _count_others(sql: ParsedSql) -> int:
    count = 0
    # number of aggregation
    agg_count = _count_agg(sql["select"][1])
    agg_count += _count_agg(sql["where"][::2])
    agg_count += _count_agg(sql["groupBy"])
    if len(sql["orderBy"]) > 0:
        agg_count += _count_agg(
            [unit[1] for unit in sql["orderBy"][1] if unit[1]]
            + [unit[2] for unit in sql["orderBy"][1] if unit[2]]
        )
    agg_count += _count_agg(sql["having"])
    if agg_count > 1:
        count += 1

    # number of select columns
    if len(sql["select"][1]) > 1:
        count += 1

    # number of where conditions
    if len(sql["where"]) > 1:
        count += 1

    # number of group by clauses
    if len(sql["groupBy"]) > 1:
        count += 1

    return count


def spider_hardness(sql: ParsedSql) -> Hardness:
    """The official Spider hardness of one parsed query (a ``dev.json`` record's ``sql``)."""
    comp1 = _count_component1(sql)
    comp2 = _count_component2(sql)
    others = _count_others(sql)

    if comp1 <= 1 and others == 0 and comp2 == 0:
        return "easy"
    if (others <= 2 and comp1 <= 1 and comp2 == 0) or (comp1 <= 2 and others < 2 and comp2 == 0):
        return "medium"
    if (
        (others > 2 and comp1 <= 2 and comp2 == 0)
        or (2 < comp1 <= 3 and others <= 2 and comp2 == 0)
        or (comp1 <= 1 and others == 0 and comp2 <= 1)
    ):
        return "hard"
    return "extra"
