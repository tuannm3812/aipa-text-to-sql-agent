from __future__ import annotations

import itertools
import math
import random
from decimal import Decimal
from typing import Any

import pytest

from text_to_sql_agent.evaluation import SCORER_V1_VERSION, rows_match
from text_to_sql_agent.evaluation_v2 import (
    SCORER_V2_VERSION,
    comparator,
    gold_has_order_by,
    rows_equal_v2,
)

Rows = list[tuple[Any, ...]]


def test_scorer_versions_are_distinct() -> None:
    assert (SCORER_V1_VERSION, SCORER_V2_VERSION) == ("1", "2")


@pytest.mark.parametrize(
    ("generated", "gold", "ordered"),
    [
        ([("A",)], [("a",)], False),  # text case
        ([(None,)], [("None",)], False),  # NULL vs text
        ([(10.004,)], [(10.0,)], False),  # precision
        ([(2,), (1,)], [(1,), (2,)], True),  # order under ORDER BY
        ([("1",)], [(1,)], False),  # text vs number
    ],
)
def test_v2_rejects_what_v1_accepted(generated: Rows, gold: Rows, ordered: bool) -> None:
    assert not rows_equal_v2(generated, gold, ordered=ordered)
    assert rows_match(generated, gold)  # v1 accepts every one of these


def test_both_versions_reject_an_extra_duplicate_row() -> None:
    generated: Rows = [("x",), ("x",)]
    gold: Rows = [("x",)]
    assert not rows_equal_v2(generated, gold, ordered=False)
    assert not rows_match(generated, gold)  # v1 keeps multiplicity too


def test_duplicates_count_on_both_sides() -> None:
    assert rows_equal_v2([("x",), ("x",), ("y",)], [("y",), ("x",), ("x",)], ordered=False)
    assert not rows_equal_v2([("x",), ("y",), ("y",)], [("x",), ("x",), ("y",)], ordered=False)


def test_v2_integer_valued_float_equals_integer() -> None:
    assert rows_equal_v2([(1.0,)], [(1,)], ordered=False)


def test_v2_relative_tolerance() -> None:
    assert rows_equal_v2([(0.1 + 0.2,)], [(0.3,)], ordered=False)
    assert rows_equal_v2([(Decimal("2.50"),)], [(2.5,)], ordered=False)


def test_tolerance_is_relative_for_large_values() -> None:
    # 1e-6 * 12_345_678.91 is about 12.3, so a 0.01 difference is inside it.
    assert rows_equal_v2([(12_345_678.92,)], [(12_345_678.91,)], ordered=False)
    # Below magnitude 1 the floor is absolute: 1e-6.
    assert rows_equal_v2([(0.0000005,)], [(0.0,)], ordered=False)
    assert not rows_equal_v2([(0.000002,)], [(0.0,)], ordered=False)


def test_integer_valued_decimal_equals_integer() -> None:
    assert rows_equal_v2([(Decimal("2.00"),)], [(2,)], ordered=False)


@pytest.mark.parametrize(
    ("generated", "gold", "equal"),
    [
        (10_000_001, 10_000_000, False),  # both integral: exact, a COUNT(*) off by one
        (Decimal("2.00"), 2, True),  # integral Decimal is an integer
        (10_000_001.0, 10_000_000, False),  # integral float too
        (1.0, Decimal("1.00"), True),
        (100.00000001, 100, True),  # one side non-integral: tolerant
        (100.001, 100, False),  # outside the tolerance
        (0.1 + 0.2, 0.3, True),
        (Decimal("10000000.5"), 10_000_000, True),  # non-integral, within 1e-6 relative
    ],
)
def test_integral_numbers_compare_exactly_otherwise_tolerantly(
    generated: Any, gold: Any, equal: bool
) -> None:
    assert rows_equal_v2([(generated,)], [(gold,)], ordered=False) is equal
    assert rows_equal_v2([(generated,)], [(gold,)], ordered=True) is equal


@pytest.mark.parametrize(
    ("generated", "gold", "equal"),
    [
        # A float/float pair is always tolerant: past 2^52 every float is "integral", but a
        # large SUM still differs by an ulp with summation order.
        (float(2**53), float(2**53 + 2), True),
        (4.6e15, 4.6e15 + 1, True),
        (1e300, 1e300 * (1 + 1e-9), True),
        (float(2**53), float(2**53 + 2**40), False),  # tolerant still means within 1e-6
        # An int or Decimal side keeps an integral pair exact.
        (10_000_000, 10_000_001, False),
        (10_000_000, 10_000_001.0, False),
        (Decimal("10000000"), 10_000_001.0, False),
        (2**53, float(2**53), True),
        (1, 1.0, True),
    ],
)
def test_exactness_needs_an_int_or_decimal_side(generated: Any, gold: Any, equal: bool) -> None:
    assert rows_equal_v2([(generated,)], [(gold,)], ordered=False) is equal
    assert rows_equal_v2([(gold,)], [(generated,)], ordered=True) is equal


@pytest.mark.parametrize(
    ("generated", "gold", "equal"),
    [
        (math.inf, math.inf, True),
        (Decimal("Infinity"), math.inf, True),
        (math.inf, -math.inf, False),
        (math.inf, 10**400, False),  # an int too large for a float is still finite
        (Decimal("1E+400"), math.inf, False),  # float() overflows silently to inf
        (Decimal("1E+400"), Decimal("1E+400"), True),
        (Decimal("1E+400"), 10**400, True),
        (Decimal("1.5E+400"), 1.5, False),
        (math.nan, math.nan, False),  # NaN equals nothing, itself included
        (math.nan, Decimal("NaN"), False),
        (Decimal("sNaN"), Decimal("sNaN"), False),  # float() would raise on sNaN
        (Decimal("sNaN"), 1.0, False),
    ],
)
def test_non_finite_and_overflowing_numbers(generated: Any, gold: Any, equal: bool) -> None:
    assert rows_equal_v2([(generated,)], [(gold,)], ordered=False) is equal
    assert rows_equal_v2([(gold,)], [(generated,)], ordered=True) is equal


def test_nan_rows_never_match_but_do_not_crash_the_sort() -> None:
    rows: Rows = [(Decimal("sNaN"), 1.0), (math.nan, 2.0)]
    assert not rows_equal_v2(rows, rows, ordered=False)


def test_bool_is_its_own_type() -> None:
    assert not rows_equal_v2([(True,)], [(1,)], ordered=False)
    assert not rows_equal_v2([(False,)], [(0.0,)], ordered=False)
    assert rows_equal_v2([(True,)], [(True,)], ordered=False)
    assert not rows_equal_v2([(True,)], [(False,)], ordered=False)


def test_null_equals_only_null() -> None:
    assert rows_equal_v2([(None,)], [(None,)], ordered=False)
    assert not rows_equal_v2([(None,)], [("",)], ordered=False)
    assert not rows_equal_v2([(None,)], [(0,)], ordered=False)


def test_text_is_trimmed_but_otherwise_exact() -> None:
    assert rows_equal_v2([("  Alice ",)], [("Alice",)], ordered=False)
    assert not rows_equal_v2([("Alice",)], [("alice",)], ordered=False)


def test_huge_integers_do_not_overflow() -> None:
    big = 10**400
    assert rows_equal_v2([(big,)], [(big,)], ordered=False)
    assert not rows_equal_v2([(big,)], [(-big,)], ordered=False)
    assert not rows_equal_v2([(big,)], [(1.0,)], ordered=False)


def test_v2_unordered_without_order_by() -> None:
    assert rows_equal_v2([(2,), (1,)], [(1,), (2,)], ordered=False)


def test_ordered_comparison_is_positional() -> None:
    assert rows_equal_v2([(1,), (2,)], [(1,), (2.0,)], ordered=True)
    assert not rows_equal_v2([(1,), (2,)], [(1,)], ordered=True)


def test_v2_column_count_must_match() -> None:
    assert not rows_equal_v2([(1, 2)], [(1,)], ordered=False)
    assert not rows_equal_v2([(1, 2)], [(1,)], ordered=True)


def test_empty_results_are_equal() -> None:
    assert rows_equal_v2([], [], ordered=False)
    assert rows_equal_v2([], [], ordered=True)
    assert not rows_equal_v2([], [(1,)], ordered=False)


def test_numeric_tie_with_differing_text_needs_a_matching() -> None:
    # Codex, 2026-10-09: a relative tolerance is not a total order. Sorting both
    # sides and comparing positionally pairs (1.0, "a") with (1.0, "b") and fails.
    gold: Rows = [(1.0, "b"), (1.0000001, "a")]
    generated: Rows = [(1.0, "a"), (1.0000001, "b")]
    assert rows_equal_v2(generated, gold, ordered=False)


# The same tie with a numeric second column: no exact cell separates the rows into
# buckets, so the matcher itself has to find the pairing.
TIE_GOLD: Rows = [(1.0, 5.0), (1.0000001, 9.0)]
TIE_GENERATED: Rows = [(1.0, 9.0), (1.0000001, 5.0)]


def test_numeric_tie_inside_one_bucket_needs_a_matching() -> None:
    assert rows_equal_v2(TIE_GENERATED, TIE_GOLD, ordered=False)
    assert not rows_equal_v2([(1.0, 9.0), (1.0000001, 9.0)], TIE_GOLD, ordered=False)


def test_ambiguous_candidates_are_resolved_exactly_not_greedily() -> None:
    # Generated row g0 is within tolerance of both gold rows; g1 matches only
    # gold[0]. A greedy first-fit pairs g0 with gold[0] and strands g1.
    gold: Rows = [(1.0,), (1.0000015,)]
    generated: Rows = [(1.0000008,), (0.9999995,)]
    assert rows_equal_v2(generated, gold, ordered=False)
    # The sorted fast path happens to accept this one, so pin the matcher too.
    assert comparator._perfect_matching_exists(generated, gold)
    assert not comparator._perfect_matching_exists(generated[::-1][:1] * 2, gold)
    # And a set that really has no perfect matching is still rejected.
    assert not rows_equal_v2([(1.0000008,), (1.0000008,)], [(1.0,), (1.0000025,)], ordered=False)


def test_sorted_fast_path_is_never_the_only_answer() -> None:
    # Pin that the exact matcher, not the sorted positional fast path, decides
    # the cases the fast path gets wrong.
    for generated, gold in [
        ([(1.0, "a"), (1.0000001, "b")], [(1.0, "b"), (1.0000001, "a")]),
        (TIE_GENERATED, TIE_GOLD),
    ]:
        assert not comparator._sorted_positional_equal(generated, gold)
        assert rows_equal_v2(generated, gold, ordered=False)


def _brute_force(generated: Rows, gold: Rows) -> bool:
    if len(generated) != len(gold):
        return False
    return any(
        all(comparator._row_equal(g, gold[j]) for g, j in zip(generated, perm, strict=True))
        for perm in itertools.permutations(range(len(gold)))
    )


@pytest.mark.parametrize("second", ["text", "number"])
def test_matching_agrees_with_brute_force_on_random_near_ties(second: str) -> None:
    # First-column values sit within and just outside tolerance of each other, so
    # most rows have several candidates. A text second column is bucketed; a
    # numeric one is not, which puts the whole set in one bucket for the matcher.
    rng = random.Random(20261009)
    values = [1.0, 1.0000006, 1.0000012, 1.0000019, 1.000003]
    seconds: list[Any] = ["a", "b"] if second == "text" else [5.0, 5.0000004, 9.0]
    for _ in range(400):
        n = rng.randint(1, 6)
        gold = [(rng.choice(values), rng.choice(seconds)) for _ in range(n)]
        generated = [(rng.choice(values), rng.choice(seconds)) for _ in range(n)]
        expected = _brute_force(generated, gold)
        assert rows_equal_v2(generated, gold, ordered=False) is expected
        assert comparator._perfect_matching_exists(generated, gold) is expected


# Groups of values that are near or exactly equal, plus confusers of another kind, so
# random rows land on both sides of every type rule.
_MIXED_GROUPS: list[list[Any]] = [
    [None, "None", ""],
    [True, False, 1, 0],
    [1, 1.0, Decimal("1.00"), True, "1"],
    [1.0, 1.0000006, 1.0000012, 1.0000019, Decimal("1.0000006")],
    [2**53, float(2**53), float(2**53 + 2), 2**53 + 1],
    [10_000_000, 10_000_000.5, 10_000_001, Decimal("10000000.5"), 10_000_001.0],
    ["A", "a", " A ", "A  "],
]


def _mixed_row(rng: random.Random, groups: list[list[Any]]) -> tuple[Any, ...]:
    return tuple(rng.choice(group) for group in groups)


def test_matching_agrees_with_brute_force_on_mixed_types() -> None:
    rng = random.Random(9006805)
    outcomes = {True: 0, False: 0}
    for _ in range(400):
        n = rng.randint(1, 5)
        shapes = [rng.sample(_MIXED_GROUPS, rng.randint(1, 2)) for _ in range(n)]
        gold = [_mixed_row(rng, shape) for shape in shapes]
        # Mostly the same shapes (so equal sets occur), sometimes a different row length.
        generated = [
            _mixed_row(rng, shape if rng.random() < 0.9 else rng.sample(_MIXED_GROUPS, 1))
            for shape in shapes
        ]
        rng.shuffle(generated)
        expected = _brute_force(generated, gold)
        outcomes[expected] += 1
        assert rows_equal_v2(generated, gold, ordered=False) is expected
        assert comparator._perfect_matching_exists(generated, gold) is expected
    assert min(outcomes.values()) >= 20  # the fixture exercises both answers


def test_large_dense_bucket_goes_through_the_matcher() -> None:
    # Every first-column value is within tolerance of every other, so each row has
    # about n/2 candidates and the sorted fast path fails: Hopcroft-Karp decides.
    rng = random.Random(7)
    n = 300
    gold = [(1.0 + rng.random() * 1e-7, rng.choice([5.0, 9.0])) for _ in range(n)]
    generated = [(1.0 + rng.random() * 1e-7, second) for _, second in gold]
    rng.shuffle(generated)
    assert not comparator._sorted_positional_equal(generated, gold)
    assert rows_equal_v2(generated, gold, ordered=False)
    flipped = next(i for i in reversed(range(n)) if generated[i][1] == 5.0)
    generated[flipped] = (generated[flipped][0], 9.0)  # one 5.0 too few: no matching
    assert not rows_equal_v2(generated, gold, ordered=False)


def test_groups_over_the_limit_fall_back_to_sorted_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Over the limit the fallback is sorted positional comparison: it can only
    # reject an equal set, never accept an unequal one.
    monkeypatch.setattr(comparator, "EXACT_MATCHING_LIMIT", 1)
    assert not rows_equal_v2(TIE_GENERATED, TIE_GOLD, ordered=False)  # equal, but rejected
    assert rows_equal_v2([(2.0, "a"), (1.0, "a")], [(1.0, "a"), (2.0, "a")], ordered=False)
    assert not rows_equal_v2([(1.0, 9.0), (1.0000001, 9.0)], TIE_GOLD, ordered=False)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM t ORDER BY a", True),
        ("SELECT a FROM t", False),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a) s", False),  # nested only
        ("WITH c AS (SELECT a FROM t) SELECT a FROM c ORDER BY a DESC", True),
        ("WITH c AS (SELECT a FROM t ORDER BY a) SELECT a FROM c", False),  # CTE body only
        ("SELECT a FROM t UNION SELECT a FROM u ORDER BY a", True),
        ("SELECT a FROM t UNION SELECT a FROM (SELECT a FROM u ORDER BY a)", False),
        ("SELECT a FROM t ORDER BY a LIMIT 3;", True),
        ("SELECT a FROM t WHERE a IN (SELECT b FROM u ORDER BY b)", False),
    ],
)
def test_gold_has_order_by(sql: str, expected: bool) -> None:
    assert gold_has_order_by(sql) is expected


@pytest.mark.parametrize("sql", ["", "SELECT 1; SELECT 2", "NOT SQL AT ALL ((("])
def test_gold_has_order_by_refuses_what_it_cannot_parse_as_one_statement(sql: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        gold_has_order_by(sql)
