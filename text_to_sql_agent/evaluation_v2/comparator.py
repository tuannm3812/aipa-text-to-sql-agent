"""The v2 result-set comparator: typed cells, multiset rows, one-to-one unordered matching.

Policy (spec §4.3): ``NULL`` equals only ``NULL``; ``bool`` is its own type; numbers compare
across ``int``/``float``/``Decimal`` under ``abs(a - b) <= 1e-6 * max(1, abs(a), abs(b))``;
text compares exactly after trimming; text never equals a number. Column count must match and
columns compare positionally. Rows are a multiset.

Unordered equality is a **perfect one-to-one matching** between generated and gold rows under
``_row_equal``. A relative tolerance is not transitive, so it is not a total order and
sort-then-compare is wrong in general: ``[(1.0, "a"), (1.0000001, "b")]`` against
``[(1.0, "b"), (1.0000001, "a")]`` is equal, but sorting pairs ``(1.0, "a")`` with
``(1.0, "b")``. The algorithm, per call:

1. **Bucket by exact cells.** Each row gets a key: per cell its kind, plus the value for
   exact kinds (``None``, ``bool``, trimmed text, hashable other values). Numbers contribute
   only their kind - *every* number is tolerant, integers included, because the spec applies
   the tolerance to all numbers. Two rows can only be equal if their keys are equal, so the
   key multisets must agree and a matching exists iff one exists inside every bucket. A bucket
   with no tolerant cell is settled by its count alone.
2. **Fast path.** Inside a bucket that has tolerant cells, sort both sides by those cells and
   compare positionally. Success is proof (the positional pairing *is* a perfect matching), so
   it returns ``True``; failure proves nothing and falls through.
3. **Exact matching.** Build the bucket's candidate lists (``O(g^2)`` row comparisons for a
   bucket of ``g`` rows) and run Hopcroft-Karp, ``O(E * sqrt(g))`` with ``E <= g^2``. This is
   exact - augmenting paths revise earlier pairings, so an ambiguous candidate set is never
   resolved greedily - and polynomial, unlike backtracking, whose worst case on a bucket of
   near-tied rows is factorial.

**Bound and guard.** Execution caps a result at ``max_rows`` (default 1,000), so ``g <= 1,000``
at default settings: at most 10^6 row comparisons and about 3.2 * 10^7 matching steps in the
adversarial worst case, seconds rather than a hang. Measured on a dense 1,000-row bucket (every
row within tolerance of half the others, fast path failing): about 0.7 s, almost all of it
building the candidate lists. A bucket larger than
``EXACT_MATCHING_LIMIT`` (1,000, so never at the default ``max_rows``) skips step 3 and keeps
the step-2 answer. That fallback is **sound but incomplete**: it never accepts an unequal pair
of result sets, but it may reject an equal set whose rows tie under tolerance - it can only
lower accuracy, never flatter it.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Hashable, Sequence
from decimal import Decimal
from numbers import Integral, Real
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

RELATIVE_TOLERANCE = 1e-6
EXACT_MATCHING_LIMIT = 1_000

Row = Sequence[Any]

_NULL, _BOOL, _NUM, _TEXT, _OTHER = "null", "bool", "num", "text", "other"


def _kind(value: Any) -> str:
    exact_type = type(value)  # fast path for what SQLite returns; the isinstance chain is ABCs
    if exact_type is int or exact_type is float:
        return _NUM
    if exact_type is str:
        return _TEXT
    if value is None:
        return _NULL
    if isinstance(value, bool):  # before numbers: bool is a subclass of int
        return _BOOL
    if isinstance(value, (Real, Decimal)):
        return _NUM
    if isinstance(value, str):
        return _TEXT
    return _OTHER


def _num_equal(a: Any, b: Any) -> bool:
    if isinstance(a, Integral) and isinstance(b, Integral):
        # Exact integer arithmetic: float() overflows past 1e308 and loses precision past 2^53.
        x, y = int(a), int(b)
        return abs(x - y) * 1_000_000 <= max(1, abs(x), abs(y))
    try:
        x_f, y_f = float(a), float(b)
    except OverflowError:  # an integer too large for a float, against a non-integer
        return False
    if not (math.isfinite(x_f) and math.isfinite(y_f)):
        return x_f == y_f or (math.isnan(x_f) and math.isnan(y_f))
    return abs(x_f - y_f) <= RELATIVE_TOLERANCE * max(1.0, abs(x_f), abs(y_f))


def _cell_equal(a: Any, b: Any) -> bool:
    kind = _kind(a)
    if kind != _kind(b):
        return False
    if kind == _NULL:
        return True
    if kind == _NUM:
        return _num_equal(a, b)
    if kind == _TEXT:
        return bool(a.strip() == b.strip())
    return bool(a == b)  # bool and other values: exact


def _row_equal(a: Row, b: Row) -> bool:
    return len(a) == len(b) and all(_cell_equal(x, y) for x, y in zip(a, b, strict=True))


def _is_tolerant(value: Any) -> bool:
    kind = _kind(value)
    return kind == _NUM or (kind == _OTHER and not isinstance(value, Hashable))


def _cell_key(value: Any) -> tuple[Any, ...]:
    kind = _kind(value)
    if kind == _TEXT:
        return (kind, value.strip())
    if kind == _BOOL or (kind == _OTHER and isinstance(value, Hashable)):
        return (kind, value)
    return (kind,)  # null, numbers and unhashable values: compared by _row_equal


def _row_key(row: Row) -> tuple[tuple[Any, ...], ...]:
    return tuple(_cell_key(value) for value in row)


def _sort_cell(value: Any) -> tuple[int, float, str]:
    if _kind(value) != _NUM:
        return (2, 0.0, repr(value))
    try:
        number = float(value)
    except OverflowError:
        number = math.inf if value > 0 else -math.inf
    return (1, 0.0, "") if math.isnan(number) else (0, number, "")


def _sort_key(row: Row) -> tuple[tuple[int, float, str], ...]:
    return tuple(_sort_cell(value) for value in row if _is_tolerant(value))


def _sorted_positional_equal(generated: Sequence[Row], gold: Sequence[Row]) -> bool:
    """Sound, incomplete: ``True`` proves equality, ``False`` proves nothing."""
    if len(generated) != len(gold):
        return False
    return all(
        _row_equal(a, b)
        for a, b in zip(sorted(generated, key=_sort_key), sorted(gold, key=_sort_key), strict=True)
    )


def _perfect_matching_exists(generated: Sequence[Row], gold: Sequence[Row]) -> bool:
    """Hopcroft-Karp over the candidate graph; exact, ``O(E * sqrt(n))``."""
    n = len(generated)
    if n != len(gold):
        return False
    adjacency: list[list[int]] = []
    for row in generated:
        candidates = [j for j, other in enumerate(gold) if _row_equal(row, other)]
        if not candidates:
            return False
        adjacency.append(candidates)

    unmatched = -1
    match_left = [unmatched] * n  # generated index -> gold index
    match_right = [unmatched] * n  # gold index -> generated index
    matched = 0
    while True:
        # BFS: layer the free generated rows and the rows reachable by alternating paths.
        layer = [math.inf] * n
        queue: deque[int] = deque()
        for u in range(n):
            if match_left[u] == unmatched:
                layer[u] = 0
                queue.append(u)
        found_free = False
        while queue:
            u = queue.popleft()
            for v in adjacency[u]:
                w = match_right[v]
                if w == unmatched:
                    found_free = True
                elif layer[w] == math.inf:
                    layer[w] = layer[u] + 1
                    queue.append(w)
        if not found_free:
            return matched == n

        # DFS, iterative (a recursive one would hit the interpreter's limit near 1,000 rows):
        # vertex-disjoint shortest augmenting paths along the BFS layers.
        next_edge = [0] * n
        for root in range(n):
            if match_left[root] != unmatched:
                continue
            stack = [root]
            via: list[int] = []  # via[i]: the gold row taken from stack[i] to stack[i + 1]
            while stack:
                u = stack[-1]
                advanced = False
                while next_edge[u] < len(adjacency[u]):
                    v = adjacency[u][next_edge[u]]
                    next_edge[u] += 1
                    w = match_right[v]
                    if w == unmatched:
                        for left, right in zip(stack, [*via, v], strict=True):
                            match_left[left] = right
                            match_right[right] = left
                        matched += 1
                        stack = []
                        advanced = True
                        break
                    if layer[w] == layer[u] + 1:
                        via.append(v)
                        stack.append(w)
                        advanced = True
                        break
                if not advanced:
                    layer[u] = math.inf  # dead end for the rest of this phase
                    stack.pop()
                    if via:
                        via.pop()


def _tolerant_equal(generated: Sequence[Row], gold: Sequence[Row]) -> bool:
    if _sorted_positional_equal(generated, gold):
        return True
    if len(generated) > EXACT_MATCHING_LIMIT:
        return False  # the documented fallback: keep the sorted comparison's answer
    return _perfect_matching_exists(generated, gold)


def _is_exact_key(cell_key: tuple[Any, ...]) -> bool:
    return len(cell_key) == 2 or cell_key[0] == _NULL


def _unordered_equal(generated: Sequence[Row], gold: Sequence[Row]) -> bool:
    buckets: defaultdict[tuple[tuple[Any, ...], ...], tuple[list[Row], list[Row]]]
    buckets = defaultdict(lambda: ([], []))
    try:
        for row in generated:
            buckets[_row_key(row)][0].append(row)
        for row in gold:
            buckets[_row_key(row)][1].append(row)
    except TypeError:  # a Hashable value whose __hash__ raises: no bucketing at all
        return _tolerant_equal(generated, gold)
    for key, (left, right) in buckets.items():
        if len(left) != len(right):
            return False
        if all(_is_exact_key(cell_key) for cell_key in key):
            continue  # every cell exact: equal keys mean equal rows
        if not _tolerant_equal(left, right):
            return False
    return True


def rows_equal_v2(
    generated: list[tuple[Any, ...]], gold: list[tuple[Any, ...]], *, ordered: bool
) -> bool:
    """Whether a generated result set equals the gold one under the v2 policy.

    Args:
        generated: Rows produced by the model's SQL.
        gold: Rows produced by the reference SQL.
        ordered: Compare rows positionally (the gold SQL has a top-level ``ORDER BY``);
            otherwise rows are a multiset matched one-to-one.

    Returns:
        True if the two result sets are equal; see the module docstring for the policy.
    """
    if len(generated) != len(gold):
        return False
    if ordered:
        return all(_row_equal(a, b) for a, b in zip(generated, gold, strict=True))
    return _unordered_equal(generated, gold)


def _outermost_query(node: exp.Expression) -> exp.Expression:
    # A whole statement wrapped in parentheses orders its result with the inner ORDER BY.
    while isinstance(node, exp.Subquery) and node.args.get("order") is None:
        node = node.this
    return node


def gold_has_order_by(sql: str) -> bool:
    """Whether the outermost statement of ``sql`` has an ``ORDER BY`` (Spider's rule).

    An ``ORDER BY`` inside a subquery, a CTE body or one arm of a set operation does not order
    the result and does not count.

    Args:
        sql: Gold SQL, SQLite dialect. Gold reaches the comparator only after ``run_gold``'s
            ``is_safe_query``, which itself requires exactly one parseable statement.

    Returns:
        True if the outermost ``SELECT`` or set operation carries an ``ORDER BY``.

    Raises:
        ValueError: If ``sql`` does not parse to exactly one statement.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as exc:
        raise ValueError(f"gold SQL must parse to exactly one statement: {exc}") from exc
    if len(statements) != 1:
        raise ValueError(f"gold SQL must parse to exactly one statement, got {len(statements)}")
    return _outermost_query(statements[0]).args.get("order") is not None
