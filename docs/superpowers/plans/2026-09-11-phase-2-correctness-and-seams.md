# Phase 2 — Correctness and Seams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the safety and execution correctness defects, remove the duplicated evaluation logic, and split the 759-line `app.py` into `ui/` modules behind a thin entrypoint.

**Architecture:** Eight tasks. The two behaviour fixes come first and are test-driven, because they are the only changes in this phase that could do harm. The missing pipeline tests land before the `app.py` split, so the functions the split moves callers of are already covered. The split itself is deliberately move-only and lands separately from every logic change, so a bisect can isolate it.

**Tech Stack:** Python 3.11-3.13, `uv`, ruff 0.16.4, mypy, pytest 8, sqlglot, Streamlit, SQLite.

## Global Constraints

- **This phase changes behaviour.** Every behaviour change ships with a test that fails before the change and passes after. Write the test first and watch it fail — a test never observed failing proves nothing.
- **Commit directly to `tuannm3812/main-refinement`.** Single-owner project, no feature branches. **Never `git commit --amend`** — commit forward only.
- **Conventional Commits**, scoped and imperative, with material detail in the body: what changed, what was verified, what was not.
- **Run `git status --short` and review every path before staging.** Never `git add -A` unpathed.
- **Never claim a check passed without running it and reading the output.**
- The system `python3` is **3.9.6** and cannot parse this codebase. Every command goes through `uv run`.
- The four gates must be green at the end of every task: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.
- `line-length = 100`, `target-version = "py311"`. `text_to_sql_agent/` stays `mypy --strict`.
- Error codes returned in `QueryResult.error` are `SCREAMING_SNAKE_CASE` constants, never free-form prose — `app.py` and the evaluation harness branch on them (`docs/0_coding_standards.md` §3).
- `app.py` must remain the Streamlit entrypoint at the repo root with a `main()`. Streamlit Cloud runs it by name.
- The master standard is `~/Documents/GitHub/coding-standards/coding_standards.md`; project deltas are `docs/0_coding_standards.md`. Never restate the master in the latter.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `text_to_sql_agent/evaluation.py` | The single copy of gold-vs-generated comparison and case running |
| `tests/test_evaluation.py` | Tests for row comparison — currently untested anywhere |
| `ui/__init__.py` | Marks the UI package |
| `ui/constants.py` | Model lists and demo database registry |
| `ui/secrets.py` | Streamlit secret lookup and provider/model resolution |
| `ui/uploads.py` | Uploaded `.db`/`.csv` handling and active-database resolution |
| `ui/results.py` | Result-to-DataFrame, chart column heuristics, error-code messages, assistant-turn rendering |
| `ui/sidebar.py` | The sidebar, returning a frozen `Settings` |
| `ui/chat.py` | The chat loop |
| `ui/evaluation.py` | The evaluation expander |

**Modified:** `text_to_sql_agent/safety.py`, `text_to_sql_agent/execution.py`, `text_to_sql_agent/config.py`, `text_to_sql_agent/pipeline.py`, `text_to_sql_agent/__init__.py`, `app.py`, `scripts/evaluate_text_to_sql.py`, `tests/test_safety.py`, `tests/test_execution.py`, `tests/test_pipeline.py`, `pyproject.toml`, `docs/0_coding_standards.md`, `docs/3_decisions.md`, `docs/4_next_steps.md`, `docs/6_agent_log.md`.

---

### Task 1: Safety — replace the keyword regex with a multi-statement guard

The highest-value fix and the only one that could reduce safety if done wrong. Read the whole task before editing.

**Files:**
- Modify: `text_to_sql_agent/safety.py`
- Test: `tests/test_safety.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `is_safe_query(sql_string: str) -> bool` — same signature, different behaviour. Task 3 and Task 4 rely on it accepting `REPLACE()` and rejecting stacked statements.

**Background you need.** `is_safe_query` currently screens SQL with `_DANGEROUS_SQL_PATTERN`, a keyword regex, before an AST check. The regex matches keywords anywhere, including inside string literals, so it rejects legitimate queries. But it cannot simply be deleted: `sqlglot.parse_one` parses only the **first** statement, so with the regex gone `SELECT 1; DROP TABLE t` passes the AST check. The regex is the only thing catching stacked statements today, by accident. The replacement is an explicit count check using `sqlglot.parse` (plural).

- [ ] **Step 1: Record the baseline**

Run: `uv run pytest`
Expected: `20 passed, 5 subtests passed`.

- [ ] **Step 2: Write the failing tests**

Replace the whole body of `tests/test_safety.py` with:

```python
from __future__ import annotations

import pytest

import text_to_sql_agent as agent

# Read-only queries that must be allowed. The first four are rejected by the
# keyword regex today: REPLACE is a standard SQLite string function, and the
# rest carry a keyword inside a string literal.
LEGITIMATE = [
    "SELECT REPLACE(name, 'a', 'b') FROM customers",
    "SELECT * FROM orders WHERE note = 'please update'",
    "SELECT * FROM orders WHERE status = 'delete'",
    "SELECT * FROM t WHERE action = 'create'",
    "SELECT name FROM customers;",
    "SELECT * FROM t WHERE x = 'a;b'",
    "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM sales "
    "GROUP BY customer_id) SELECT * FROM totals",
    "SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 2",
    "SELECT strftime('%Y', d) AS yr, SUM(amt) FROM s GROUP BY yr",
]

# Anything that writes, inspects internals, or smuggles a second statement.
DANGEROUS = [
    "DELETE FROM customers",
    "DROP TABLE customers",
    "UPDATE customers SET name = 'x'",
    "INSERT INTO customers VALUES (1, 'x')",
    "PRAGMA table_info(customers)",
    "ATTACH DATABASE 'other.db' AS other",
    "VACUUM",
    "REINDEX",
    "CREATE TABLE x (a INTEGER)",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM sqlite_schema",
    "SELECT 1; DROP TABLE customers",
    "SELECT * FROM t; DELETE FROM t;",
    "SELECT 1; SELECT 2",
    "WITH x AS (SELECT 1) DELETE FROM t",
    "",
    "   ",
]


@pytest.mark.parametrize("sql", LEGITIMATE)
def test_is_safe_query_allows_read_only_queries(sql: str) -> None:
    assert agent.is_safe_query(sql), f"should have been allowed: {sql!r}"


@pytest.mark.parametrize("sql", DANGEROUS)
def test_is_safe_query_blocks_unsafe_sql(sql: str) -> None:
    assert not agent.is_safe_query(sql), f"should have been blocked: {sql!r}"


def test_is_safe_query_rejects_stacked_statements_even_when_all_are_selects() -> None:
    """The AST check only ever sees the first statement, so count them explicitly."""
    assert not agent.is_safe_query("SELECT 1; SELECT 2")


def test_is_safe_query_fails_closed_when_sqlglot_is_missing(monkeypatch) -> None:
    """Without a parser there is no safety check, so refuse rather than guess."""
    from text_to_sql_agent import safety

    monkeypatch.setattr(safety, "sqlglot", None)
    monkeypatch.setattr(safety, "exp", None)
    assert not safety.is_safe_query("SELECT 1")
```

- [ ] **Step 3: Run them and confirm which fail**

Run: `uv run pytest tests/test_safety.py -v`

Expected: the four keyword-in-literal cases fail (currently blocked), and
`test_is_safe_query_fails_closed_when_sqlglot_is_missing` fails (currently returns
`True`). The stacked-statement cases **pass** already — the regex catches them
today. That is exactly why you must not simply delete it.

- [ ] **Step 4: Rewrite `safety.py`**

Replace lines 1-78 entirely:

```python
"""Read-only SQL validation: structural checks plus a sqlglot AST safety check."""

from __future__ import annotations

import re
from types import ModuleType

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None

_FORBIDDEN_INTERNALS = re.compile(r"(?is)\bsqlite_master\b|\bsqlite_schema\b")
_ALLOWED_PREFIX = re.compile(r"(?is)^(select|with)\b")


def _is_safe_ast(sql_string: str) -> bool:
    """Reject anything that parses to more than one statement or writes data.

    Returns False when `sqlglot` is unavailable: without a parser there is no
    validation to perform, and refusing to run is the correct failure mode for
    a safety check.
    """
    if sqlglot is None or exp is None:
        return False
    try:
        statements = sqlglot.parse(sql_string, read="sqlite")
    except Exception:
        return False

    # `parse_one` would silently inspect only the first statement, so a payload
    # like "SELECT 1; DROP TABLE t" would be approved on the strength of its
    # harmless prefix. Count them instead.
    if len(statements) != 1:
        return False
    parsed = statements[0]
    if parsed is None:
        return False

    forbidden = (
        exp.Alter,
        exp.Command,
        exp.Create,
        exp.Delete,
        exp.Drop,
        exp.Insert,
        exp.Merge,
        exp.Update,
    )
    if isinstance(parsed, forbidden) or any(parsed.find_all(*forbidden)):
        return False

    allowed_roots = (exp.Select, exp.Union, exp.With)
    return isinstance(parsed, allowed_roots) or parsed.find(exp.Select) is not None


def is_safe_query(sql_string: str) -> bool:
    """Conservatively allow only single-statement, read-only SELECT/CTE queries.

    Rejects anything empty, not starting with `SELECT`/`WITH`, referencing
    `sqlite_master`/`sqlite_schema`, parsing to more than one statement, or
    containing a data-modifying node. Keyword matching is deliberately *not*
    used: it rejected legitimate SQL such as `REPLACE(...)` and string literals
    containing words like `update`.

    This is one of two independent defences. `execution.py` opens the database
    read-only and installs a write authorizer; neither relies on the other.

    Args:
        sql_string: The SQL text to validate.

    Returns:
        True if the query is judged safe to execute read-only. False when
        `sqlglot` is unavailable, since no validation is possible.
    """
    if not sql_string or not sql_string.strip():
        return False
    s = sql_string.strip().rstrip(";").strip()
    if not _ALLOWED_PREFIX.match(s):
        return False
    if _FORBIDDEN_INTERNALS.search(s):
        return False
    return _is_safe_ast(s)
```

- [ ] **Step 5: Run the safety tests**

Run: `uv run pytest tests/test_safety.py -v`
Expected: all pass. If any `DANGEROUS` entry now passes, **stop** — that is a
security regression, not a test to adjust.

- [ ] **Step 6: Run the whole suite and the gates**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Expected: all green. The suite count rises because `parametrize` reports one
test per case — record the new number.

- [ ] **Step 7: Prove the guard is load-bearing**

A test that would pass without your change proves nothing. Temporarily change
`if len(statements) != 1:` to `if False:`, run
`uv run pytest tests/test_safety.py`, and confirm the stacked-statement tests
**fail**. Then restore it and confirm green again.

- [ ] **Step 8: Commit**

```bash
git status --short
git add text_to_sql_agent/safety.py tests/test_safety.py
git commit -m "fix(safety): replace the keyword regex with a multi-statement guard

The regex matched keywords anywhere in the statement, including inside
string literals, so it rejected legitimate read-only SQL: REPLACE() is a
standard SQLite string function, and any literal containing update,
delete or create was blocked. Users saw BLOCKED_UNSAFE_SQL with no way to
tell it was a false alarm.

The regex could not simply be deleted. sqlglot.parse_one parses only the
first statement, so without it 'SELECT 1; DROP TABLE t' passed the AST
check on the strength of its harmless prefix - the regex was the only
thing catching stacked statements, by accident. It is replaced by an
explicit count check using sqlglot.parse.

Also flips the missing-sqlglot fallback from fail-open to fail-closed. It
returned True when the parser was absent, which was survivable while the
regex existed and is not now.

Verified: parametrised corpus of 9 legitimate and 17 dangerous inputs,
zero leaks and zero false rejects; confirmed the count check is
load-bearing by disabling it and watching the stacked-statement tests
fail.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Execution — turn the query abort into a typed result

**Files:**
- Modify: `text_to_sql_agent/execution.py`, `text_to_sql_agent/config.py`, `text_to_sql_agent/pipeline.py`
- Test: `tests/test_execution.py`, `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `is_safe_query` from Task 1.
- Produces: the error code string `QUERY_ABORTED_AFTER_<n>_VM_STEPS`; `DEFAULT_MAX_VM_STEPS` replacing `DEFAULT_SQLITE_PROGRESS_STEPS`; `execute_query(..., max_vm_steps=...)` replacing the `progress_steps` keyword. Task 5 renders the new code; Task 4 may observe it.

**Background.** `conn.set_progress_handler(lambda: 1, 100_000)` aborts any query
exceeding 100,000 VM steps, because a progress handler returning non-zero
interrupts execution. This is a genuine runaway-query guard and is being kept:
verified that it aborts a 60,000-row self-join while all 12 gold evaluation cases
pass untouched. The defect is that it escapes as a bare
`sqlite3.OperationalError: interrupted`, which `ask_database` then stringifies
into `QueryResult.error` **and** treats as a generation failure worth spending an
LLM repair call on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_execution.py`:

```python
def test_execute_query_returns_typed_error_when_aborted(tmp_path: Path) -> None:
    """A runaway query is aborted by the VM-step guard, not raised as OperationalError."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()

    result = agent.execute_query(
        str(db_path), "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"
    )

    assert not result.ok
    assert result.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"
    assert result.sql == "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"


def test_execute_query_guard_can_be_disabled(tmp_path: Path) -> None:
    """max_vm_steps=0 turns the guard off, so a slow query completes."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(2_000)])
        conn.commit()

    result = agent.execute_query(
        str(db_path), "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i", max_vm_steps=0
    )

    assert result.ok, result.error
    assert result.rows == [(2_000,)]


def test_execute_query_still_raises_real_operational_errors(tmp_path: Path) -> None:
    """A genuine SQL error must not be disguised as an abort."""
    db_path = tmp_path / "t.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()

    with pytest.raises(sqlite3.OperationalError):
        agent.execute_query(str(db_path), "SELECT * FROM table_that_does_not_exist")
```

Append to `tests/test_pipeline.py`:

```python
def test_ask_database_does_not_repair_an_aborted_query(tmp_path: Path) -> None:
    """An abort is a resource limit, not bad SQL - repairing it wastes an LLM call."""
    db_path = tmp_path / "big.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE n (i INTEGER)")
        conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(60_000)])
        conn.commit()

    runaway = "SELECT COUNT(*) FROM n a JOIN n b ON a.i = b.i"
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value=runaway) as gen:
        result = agent.ask_database("count pairs", db_path=str(db_path))

    assert gen.call_count == 1, "the repair path must not fire for an aborted query"
    assert result.error == "QUERY_ABORTED_AFTER_100000_VM_STEPS"
```

`tests/test_pipeline.py` needs `import sqlite3` and `from contextlib import closing`
added to its imports for this test.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_execution.py tests/test_pipeline.py -v`
Expected: the three new execution tests fail (two with `OperationalError:
interrupted` escaping, one with an unexpected `max_vm_steps` keyword), and the
pipeline test fails with `gen.call_count == 2`.

- [ ] **Step 3: Rename the config constant**

In `text_to_sql_agent/config.py`, replace the `DEFAULT_SQLITE_PROGRESS_STEPS`
line with:

```python
# SQLite virtual-machine steps a single query may run before it is aborted.
# This is a runaway-query guard for the hosted demo, where anyone can submit a
# query: a cross join over a large table would otherwise run unbounded. All 12
# gold evaluation cases complete well inside it. 0 disables the guard.
DEFAULT_MAX_VM_STEPS = 100_000
```

- [ ] **Step 4: Update `execution.py`**

Change the import on line 10 to
`from .config import DEFAULT_MAX_ROWS, DEFAULT_MAX_VM_STEPS`, add a named handler
above `execute_query`, and rewrite the function body:

```python
def _abort_query() -> int:
    """SQLite progress handler: a non-zero return aborts the running query.

    Installed with an interval of `max_vm_steps`, so the first callback ends a
    query that exceeds the budget.
    """
    return 1
```

```python
def execute_query(
    db_path: str,
    sql_string: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_vm_steps: int = DEFAULT_MAX_VM_STEPS,
) -> QueryResult:
    """Execute a validated SELECT against SQLite with read-only protections.

    Args:
        db_path: Filesystem path to the SQLite database.
        sql_string: A query already cleared by `is_safe_query`.
        max_rows: Maximum rows returned before the result is marked truncated.
        max_vm_steps: SQLite VM steps a query may run before it is aborted.
            `0` disables the guard.

    Returns:
        A `QueryResult`. Its `error` is `RESULT_TRUNCATED_TO_<n>_ROWS` when more
        rows were available than `max_rows` allowed, or
        `QUERY_ABORTED_AFTER_<n>_VM_STEPS` when the guard stopped the query.

    Raises:
        ValueError: If `max_rows` is less than 1.
        sqlite3.OperationalError: For genuine SQL errors, such as a missing
            table. Only the abort interrupt is converted to a returned error.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1")

    with closing(_read_only_sqlite_connection(db_path)) as conn:
        if max_vm_steps > 0:
            conn.set_progress_handler(_abort_query, max_vm_steps)
        try:
            cur = conn.execute(sql_string)
            rows = cur.fetchmany(max_rows + 1)
            columns = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc).lower():
                raise
            return QueryResult(
                columns=[],
                rows=[],
                sql=sql_string,
                error=f"QUERY_ABORTED_AFTER_{max_vm_steps}_VM_STEPS",
            )
        capped_rows = rows[:max_rows]
        if len(rows) > max_rows:
            return QueryResult(
                columns=columns,
                rows=[tuple(r) for r in capped_rows],
                sql=sql_string,
                error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS",
            )
        return QueryResult(columns=columns, rows=[tuple(r) for r in capped_rows], sql=sql_string)
```

Matching on the message text is unavoidable — `sqlite3` raises a plain
`OperationalError` for both an interrupt and a missing table, with no distinct
subclass or error code exposed. The third test in Step 1 exists to prove a real
SQL error is not swallowed by this branch.

- [ ] **Step 5: Update the export and every caller of the old name**

```bash
grep -rn "DEFAULT_SQLITE_PROGRESS_STEPS\|progress_steps" --include="*.py" --include="*.ipynb" . | grep -v "^./.git"
```

Update each hit: `text_to_sql_agent/__init__.py` imports and re-exports the
constant in both its import block and `__all__`. Keep `__all__` alphabetical.

- [ ] **Step 6: Stop `pipeline.py` repairing an aborted query**

`execute_query` no longer raises for an abort, so the `except` around it is not
reached — the returned `QueryResult` flows straight back. Confirm by reading
`ask_database` and `ask_database_with_sql` that neither inspects
`result.error` before returning. If either does, make sure an abort is returned
as-is rather than routed into `_repair_sql`.

- [ ] **Step 7: Run everything**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Expected: all green, including the four new tests.

- [ ] **Step 8: Confirm no gold case regressed**

```bash
uv run python -c "
import json
from text_to_sql_agent import execute_query
bad = []
for c in json.load(open('evaluation/cases.json')):
    r = execute_query(c['db_path'], c['gold_sql'])
    if not r.ok:
        bad.append((c['question'][:40], r.error))
print('regressions:', bad or 'none')
assert not bad
"
```

Expected: `regressions: none`.

- [ ] **Step 9: Commit**

```bash
git status --short
git add text_to_sql_agent/execution.py text_to_sql_agent/config.py text_to_sql_agent/__init__.py tests/test_execution.py tests/test_pipeline.py
git commit -m "fix(execution): return a typed error when the VM-step guard aborts a query

set_progress_handler(lambda: 1, 100_000) aborts any query exceeding
100,000 SQLite VM steps, because a progress handler returning non-zero
interrupts execution. That is a deliberate runaway-query guard for the
hosted demo and is kept: it stops a 60k-row self-join while all 12 gold
evaluation cases complete well inside the budget.

What was wrong is the reporting. The abort escaped as a bare
OperationalError: interrupted, which ask_database stringified into
QueryResult.error as meaningless text and - worse - treated as a
generation failure, spending an LLM repair call on SQL that was never
wrong. It now returns QUERY_ABORTED_AFTER_n_VM_STEPS, matching the
existing SCREAMING_SNAKE_CASE error-code convention.

Renames DEFAULT_SQLITE_PROGRESS_STEPS to DEFAULT_MAX_VM_STEPS and the
keyword to max_vm_steps, since the old names described the mechanism
rather than the intent, and names the handler instead of using a bare
lambda.

The interrupt is distinguished by message text because sqlite3 raises a
plain OperationalError for both an interrupt and a missing table; a test
asserts a genuine SQL error still propagates rather than being disguised
as an abort.

Verified: 4 new tests; repair path fires once not twice for an aborted
query; all 12 gold cases still execute clean.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Cover the untested pipeline functions

`ask_database_with_sql` is the function `app.py` actually calls and has no tests.
`ask_from_files` is the CSV-upload path. `_repair_sql` is untested. Task 6 moves
their callers, so cover them first.

**Files:**
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `QUERY_ABORTED_AFTER_<n>_VM_STEPS` from Task 2.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the tests**

Append to `tests/test_pipeline.py`:

```python
def test_ask_database_with_sql_returns_generated_sql(customers_db: str) -> None:
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"
    ):
        sql, result = agent.ask_database_with_sql("list customers", db_path=customers_db)

    assert sql == "SELECT name FROM customers"
    assert result.ok
    assert result.rows == [("Alice",)]


def test_ask_database_with_sql_returns_empty_sql_when_generation_fails(
    customers_db: str,
) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=RuntimeError("no key")):
        sql, result = agent.ask_database_with_sql("list customers", db_path=customers_db)

    assert sql == ""
    assert not result.ok
    assert "RuntimeError" in (result.error or "")


def test_ask_database_with_sql_blocks_unsafe_sql(customers_db: str) -> None:
    with patch("text_to_sql_agent.pipeline.generate_sql", return_value="DROP TABLE customers"):
        sql, result = agent.ask_database_with_sql("remove customers", db_path=customers_db)

    assert sql == "DROP TABLE customers"
    assert result.error == "BLOCKED_UNSAFE_SQL"


def test_ask_from_files_routes_a_db_path(customers_db: str) -> None:
    with patch(
        "text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"
    ):
        result = agent.ask_from_files("list customers", [customers_db])

    assert result.ok
    assert result.rows == [("Alice",)]


def test_ask_from_files_ingests_csvs_then_queries(tmp_path: Path) -> None:
    csv_path = tmp_path / "people.csv"
    csv_path.write_text("name,age\nAlice,30\nBob,41\n", encoding="utf-8")
    out_db = tmp_path / "ingested.db"

    with patch("text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM people"):
        result = agent.ask_from_files(
            "list people", [str(csv_path)], output_db_path=str(out_db)
        )

    assert result.ok, result.error
    assert result.rows == [("Alice",), ("Bob",)]


def test_ask_from_files_rejects_mixed_extensions(tmp_path: Path) -> None:
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("a\n1\n", encoding="utf-8")
    db_path = tmp_path / "b.db"
    db_path.touch()

    with pytest.raises(ValueError, match="mixed"):
        agent.ask_from_files("q", [str(csv_path), str(db_path)])


def test_ask_from_files_rejects_an_empty_file_list() -> None:
    with pytest.raises(ValueError, match="at least one"):
        agent.ask_from_files("q", [])


def test_repair_is_attempted_once_when_execution_fails(customers_db: str) -> None:
    """A genuine SQL error should trigger exactly one repair attempt."""
    responses = ["SELECT nope FROM customers", "SELECT name FROM customers"]
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses) as gen:
        result = agent.ask_database("list customers", db_path=customers_db)

    assert gen.call_count == 2, "one generation plus one repair"
    assert result.ok, result.error
    assert result.rows == [("Alice",)]


def test_repaired_sql_is_rechecked_for_safety(customers_db: str) -> None:
    """A repair that returns unsafe SQL must not be executed."""
    responses = ["SELECT nope FROM customers", "DROP TABLE customers"]
    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=responses):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert not result.ok
    with closing(sqlite3.connect(customers_db)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert ("customers",) in tables, "the table must still exist"
```

`tests/test_pipeline.py` needs `import pytest` and `from pathlib import Path`
in its imports if not already present.

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_pipeline.py -v`

Expected: all pass — these cover existing behaviour rather than driving new code.
**If any fails, that is a real bug you have just found.** Do not adjust the test
to match the behaviour; report it, and fix it only if the fix is small and
clearly correct.

- [ ] **Step 3: Gates and commit**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git status --short
git add tests/test_pipeline.py
git commit -m "test(pipeline): cover ask_database_with_sql, ask_from_files and repair

ask_database_with_sql is the function app.py actually calls and had no
tests; ask_from_files is the CSV-upload path; the repair path was
untested. Adds coverage for the generated-SQL return, the empty-SQL
generation-failure contract, unsafe-SQL blocking, both ask_from_files
routes and its two ValueError guards, and two repair cases - that exactly
one repair is attempted, and that repaired SQL is re-checked by
is_safe_query before execution.

These describe existing behaviour rather than driving new code, and land
before the app.py split moves their callers.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Extract the shared evaluation module

**Files:**
- Create: `text_to_sql_agent/evaluation.py`, `tests/test_evaluation.py`
- Modify: `text_to_sql_agent/__init__.py`, `app.py`, `scripts/evaluate_text_to_sql.py`

**Interfaces:**
- Consumes: `execute_query`, `ask_database_with_sql`, `is_safe_query`, `QueryResult`.
- Produces: `normalise_rows(rows) -> list[list[str]]`, `canonical_value(value) -> str`, `rows_match(generated, gold) -> bool`, `load_cases(path="evaluation/cases.json") -> list[dict[str, Any]]`. Task 6's `ui/evaluation.py` imports these.

**Background.** `_normalise_rows`, `_canonical_value` and `_value_rows_match` are
byte-identical (modulo annotations) in `app.py:262-280` and
`scripts/evaluate_text_to_sql.py:25-43`. They decide every accuracy number this
project reports and have **no tests in either copy**.

- [ ] **Step 1: Create the module**

```python
"""Shared gold-vs-generated comparison for the evaluation harness.

Both the Streamlit evaluation tab and `scripts/evaluate_text_to_sql.py` import
this. It previously existed as two identical private copies that could drift
apart, silently making the app and the CLI disagree about accuracy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CASES_PATH = Path("evaluation/cases.json")


def canonical_value(value: Any) -> str:
    """Normalise one cell so equivalent values compare equal.

    Numbers are rounded to two decimals so `10`, `10.0` and `10.004` match;
    everything else is stripped and lowercased.

    Args:
        value: A single cell from a result row.

    Returns:
        The canonical string form of the value.
    """
    if isinstance(value, (int, float)):
        return str(round(float(value), 2))
    text = str(value).strip()
    try:
        return str(round(float(text), 2))
    except ValueError:
        return text.lower()


def normalise_rows(rows: list[tuple[Any, ...]]) -> list[list[str]]:
    """Render rows as lists of plain strings, for display and serialisation."""
    return [[str(value) for value in row] for row in rows]


def rows_match(generated: list[tuple[Any, ...]], gold: list[tuple[Any, ...]]) -> bool:
    """Compare two result sets ignoring row order and numeric formatting.

    Row order is ignored because a question rarely constrains it, and a query
    that returns the right rows in a different order is correct.

    Args:
        generated: Rows produced by the model's SQL.
        gold: Rows produced by the reference SQL.

    Returns:
        True if the two sets contain the same canonical rows.
    """
    generated_rows = sorted(tuple(canonical_value(v) for v in row) for row in generated)
    gold_rows = sorted(tuple(canonical_value(v) for v in row) for row in gold)
    return generated_rows == gold_rows


def load_cases(path: str | Path = DEFAULT_CASES_PATH) -> list[dict[str, Any]]:
    """Load evaluation cases, returning an empty list when the file is absent.

    Args:
        path: Path to the cases JSON file.

    Returns:
        The parsed cases, or `[]` if the file does not exist.
    """
    cases_path = Path(path)
    if not cases_path.is_file():
        return []
    parsed: list[dict[str, Any]] = json.loads(cases_path.read_text(encoding="utf-8"))
    return parsed
```

- [ ] **Step 2: Write its tests**

Create `tests/test_evaluation.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from text_to_sql_agent import evaluation


def test_canonical_value_rounds_numbers_to_two_decimals() -> None:
    assert evaluation.canonical_value(10) == "10.0"
    assert evaluation.canonical_value(10.0) == "10.0"
    assert evaluation.canonical_value(10.004) == "10.0"
    assert evaluation.canonical_value("10.004") == "10.0"


def test_canonical_value_lowercases_and_strips_text() -> None:
    assert evaluation.canonical_value("  Business  ") == "business"


def test_rows_match_ignores_row_order() -> None:
    assert evaluation.rows_match([("b", 2), ("a", 1)], [("a", 1), ("b", 2)])


def test_rows_match_ignores_numeric_formatting() -> None:
    assert evaluation.rows_match([("a", 1)], [("a", 1.0)])
    assert evaluation.rows_match([("a", "1.00")], [("a", 1)])


def test_rows_match_rejects_different_values() -> None:
    assert not evaluation.rows_match([("a", 1)], [("a", 2)])


def test_rows_match_rejects_different_row_counts() -> None:
    assert not evaluation.rows_match([("a", 1)], [("a", 1), ("b", 2)])


def test_rows_match_is_true_for_two_empty_results() -> None:
    assert evaluation.rows_match([], [])


def test_normalise_rows_stringifies_every_cell() -> None:
    assert evaluation.normalise_rows([(1, "a", None)]) == [["1", "a", "None"]]


def test_load_cases_returns_empty_list_when_file_is_missing(tmp_path: Path) -> None:
    assert evaluation.load_cases(tmp_path / "nope.json") == []


def test_load_cases_parses_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"question": "q", "gold_sql": "SELECT 1"}]), encoding="utf-8")
    assert evaluation.load_cases(path) == [{"question": "q", "gold_sql": "SELECT 1"}]


def test_shipped_cases_file_loads_and_has_the_required_keys() -> None:
    cases = evaluation.load_cases()
    assert cases, "evaluation/cases.json should not be empty"
    for case in cases:
        assert {"question", "gold_sql", "db_path"} <= set(case)
```

- [ ] **Step 3: Run them**

Run: `uv run pytest tests/test_evaluation.py -v`
Expected: all pass.

- [ ] **Step 4: Export from the package**

In `text_to_sql_agent/__init__.py` add
`from .evaluation import canonical_value, load_cases, normalise_rows, rows_match`
and add all four names to `__all__`, keeping it alphabetical.

- [ ] **Step 5: Delete both duplicate copies**

In `app.py`, delete `_load_evaluation_cases`, `_normalise_rows`, `_canonical_value`
and `_value_rows_match` (lines 255-280), and repoint their call sites at
`backend.load_cases`, `backend.normalise_rows`, `backend.canonical_value` and
`backend.rows_match`.

In `scripts/evaluate_text_to_sql.py`, delete the same three functions (lines
25-43) and repoint call sites at `agent.normalise_rows`, `agent.canonical_value`
and `agent.rows_match`.

- [ ] **Step 6: Prove no copy survives**

```bash
grep -rn "_normalise_rows\|_canonical_value\|_value_rows_match\|_load_evaluation_cases" \
  --include="*.py" . | grep -v "^./.git"
```

Expected: no output.

- [ ] **Step 7: Verify the CLI still runs**

```bash
uv run python scripts/evaluate_text_to_sql.py --help
uv run python scripts/evaluate_text_to_sql.py --mode gold 2>&1 | tail -5
```

The gold mode needs no LLM. Expected: it runs the 12 cases and reports results.
If it needs a flag other than `--mode gold`, read `--help` and use the right one.

- [ ] **Step 8: Gates and commit**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git status --short
git add text_to_sql_agent/evaluation.py text_to_sql_agent/__init__.py tests/test_evaluation.py app.py scripts/evaluate_text_to_sql.py
git commit -m "refactor(evaluation): extract the one shared gold-comparison module

normalise_rows, canonical_value and value_rows_match existed twice,
byte-identical apart from annotations, in app.py and
scripts/evaluate_text_to_sql.py. They decide every accuracy number this
project reports, and had no tests in either copy - so the app and the CLI
could silently drift into disagreeing about correctness.

Now one copy in text_to_sql_agent/evaluation.py, public rather than
underscore-private, since being private is what let two copies coexist
unnoticed. Adds 11 tests, including that row order and numeric formatting
are ignored, and that the shipped cases file parses with the keys the
harness needs.

Verified: no duplicate remains; the gold-mode CLI runs the 12 cases.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Extract the pure UI helpers

The first half of the `app.py` split: everything with no Streamlit layout in it.
Move-only, plus one small addition (error-code messages) that Task 2 made
necessary.

**Files:**
- Create: `ui/__init__.py`, `ui/constants.py`, `ui/secrets.py`, `ui/uploads.py`, `ui/results.py`
- Modify: `app.py`
- Test: `tests/test_ui_results.py`

**Interfaces:**
- Consumes: `QUERY_ABORTED_AFTER_<n>_VM_STEPS` from Task 2.
- Produces: `ui.constants.GEMINI_MODELS`, `OLLAMA_MODELS`, `DEMO_DATABASES`; `ui.secrets.streamlit_secret`, `active_gemini_key`, `model_name_for_provider`; `ui.uploads.write_uploaded_db`, `write_uploaded_csvs`, `active_db_path`; `ui.results.result_to_dataframe`, `chartable_columns`, `describe_error`, `render_assistant_turn`. Task 6 imports all of these.

**Why `ui/` and not `text_to_sql_agent/ui/`:** the backend package is a reusable
library the notebook and the evaluation script import, and Streamlit presentation
code does not belong in it. `pyproject.toml` packages `text_to_sql_agent` only,
so `ui/` is not shipped in the wheel — that is deliberate. **Do not add `ui` to
`[tool.hatch.build.targets.wheel] packages`**; it would publish a
Streamlit-dependent module from a library whose consumers may not have Streamlit.

- [ ] **Step 1: Create the package and move the constants**

`ui/__init__.py` contains only a docstring:

```python
"""Streamlit presentation layer for the Text-to-SQL agent."""
```

Create `ui/constants.py` and move `GEMINI_MODELS`, `OLLAMA_MODELS` and
`DEMO_DATABASES` from `app.py:27-60` **verbatim**, adding a module docstring.
Do not re-order or edit the entries.

- [ ] **Step 2: Move the secret and model helpers**

Create `ui/secrets.py` holding `_streamlit_secret`, `_active_gemini_key` and
`_model_name_for_provider` from `app.py:146-168`, renamed without the leading
underscore (they are now a module's public API) and importing what they need
from `ui.constants` and `text_to_sql_agent`. Bodies move unchanged.

- [ ] **Step 3: Move the upload helpers**

Create `ui/uploads.py` holding `_write_uploaded_db`, `_write_uploaded_csvs` and
`_active_db_path` from `app.py:171-253`, renamed without the underscore. Bodies
move unchanged.

- [ ] **Step 4: Write the failing test for error messages**

Task 2 introduced `QUERY_ABORTED_AFTER_<n>_VM_STEPS`, and `app.py` renders
`result.error` as raw text — so a user currently sees the constant itself. Create
`tests/test_ui_results.py`:

```python
from __future__ import annotations

import pytest

from ui import results

KNOWN_CODES = [
    "BLOCKED_UNSAFE_SQL",
    "UNANSWERABLE_WITH_GIVEN_SCHEMA",
    "RESULT_TRUNCATED_TO_1000_ROWS",
    "QUERY_ABORTED_AFTER_100000_VM_STEPS",
]


@pytest.mark.parametrize("code", KNOWN_CODES)
def test_describe_error_returns_a_human_message(code: str) -> None:
    message = results.describe_error(code)
    assert message != code, f"{code} is still shown raw to the user"
    assert message[0].isupper()
    assert len(message) > 20


def test_describe_error_includes_the_row_cap_in_the_truncation_message() -> None:
    assert "1000" in results.describe_error("RESULT_TRUNCATED_TO_1000_ROWS")


def test_describe_error_includes_the_step_budget_in_the_abort_message() -> None:
    assert "100000" in results.describe_error("QUERY_ABORTED_AFTER_100000_VM_STEPS")


def test_describe_error_passes_through_an_unrecognised_message() -> None:
    assert results.describe_error("RuntimeError: boom") == "RuntimeError: boom"


def test_describe_error_handles_none() -> None:
    assert results.describe_error(None) == ""
```

Run: `uv run pytest tests/test_ui_results.py -v` — expected: fails, no module `ui.results`.

- [ ] **Step 5: Create `ui/results.py`**

Move `_result_to_dataframe`, `_chartable_columns` and `_render_assistant_turn`
from `app.py:188-206` and `app.py:349-387` **verbatim** apart from dropping the
leading underscore, then add:

```python
_ERROR_MESSAGES = {
    "BLOCKED_UNSAFE_SQL": (
        "The generated SQL was blocked because it was not a read-only query. "
        "The SQL is shown below so you can see what was rejected."
    ),
    "UNANSWERABLE_WITH_GIVEN_SCHEMA": (
        "The question could not be answered from this database's schema. "
        "Try rephrasing it, or pick a database that holds the relevant tables."
    ),
}

_TRUNCATED_PREFIX = "RESULT_TRUNCATED_TO_"
_ABORTED_PREFIX = "QUERY_ABORTED_AFTER_"


def describe_error(code: str | None) -> str:
    """Turn a `QueryResult.error` code into a message for a non-technical reader.

    Anything unrecognised is passed through unchanged, since the backend also
    puts raw exception text in this field.

    Args:
        code: The `QueryResult.error` value, or None.

    Returns:
        A human-readable message, or `""` when `code` is None.
    """
    if not code:
        return ""
    if code in _ERROR_MESSAGES:
        return _ERROR_MESSAGES[code]
    if code.startswith(_TRUNCATED_PREFIX):
        limit = code[len(_TRUNCATED_PREFIX) :].removesuffix("_ROWS")
        return (
            f"Showing the first {limit} rows. The query matched more than that, "
            "so add a filter or an aggregate to narrow it down."
        )
    if code.startswith(_ABORTED_PREFIX):
        budget = code[len(_ABORTED_PREFIX) :].removesuffix("_VM_STEPS")
        return (
            f"The query was stopped after {budget} database steps to keep the demo "
            "responsive. It was probably joining tables without a matching condition."
        )
    return code
```

Then use it in `render_assistant_turn`, replacing `st.warning(msg["error_text"])`
and `st.error(msg["error_text"])` with `describe_error(msg["error_text"])`.

- [ ] **Step 6: Run the test**

Run: `uv run pytest tests/test_ui_results.py -v`
Expected: all pass.

- [ ] **Step 7: Repoint `app.py` and delete the moved code**

Delete the moved definitions from `app.py` and import from the new modules.
`app.py` still holds `main()` at this stage — Task 6 splits that.

- [ ] **Step 8: Verify the app still imports and answers**

```bash
uv run python -c "
import importlib, re
import text_to_sql_agent as b
importlib.import_module('app')
print('app imports cleanly')
from unittest.mock import patch
with patch('text_to_sql_agent.pipeline.generate_sql',
           return_value='SELECT major, COUNT(*) AS n FROM students GROUP BY major'):
    r = b.ask_database('students per major', db_path='data/university_agent.db')
assert r.ok, r.error
print('end-to-end OK:', len(r.rows), 'rows')
"
```

- [ ] **Step 9: Gates and commit**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git status --short
git add ui app.py tests/test_ui_results.py
git commit -m "refactor(ui): extract constants, secrets, uploads and result helpers

First half of the app.py split. Moves everything with no Streamlit layout
in it into a ui/ package, verbatim apart from dropping leading
underscores now that these are module APIs.

ui/ sits beside app.py rather than inside text_to_sql_agent/: the backend
package is a library the notebook and the evaluation script import, and
presentation code does not belong in it. It is deliberately not added to
the wheel packages, which would publish a Streamlit-dependent module to
consumers that may not have Streamlit.

Adds ui.results.describe_error, which is not a pure move. app.py rendered
QueryResult.error as raw text, so users saw constants like
BLOCKED_UNSAFE_SQL; the new QUERY_ABORTED_AFTER_n_VM_STEPS code from the
execution fix would have been shown the same way. Unrecognised values
still pass through, since the backend also puts raw exception text there.

Verified: app.py imports cleanly and a mocked end-to-end query returns
rows; 6 new tests for the message mapping.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Split `main()` and make `app.py` thin

The largest task, and deliberately the most mechanical. **Move code; do not
rewrite it.** The only intentional change is replacing sidebar-to-body
communication through `st.session_state` string keys with a returned dataclass.

**Files:**
- Create: `ui/settings.py`, `ui/sidebar.py`, `ui/chat.py`, `ui/evaluation.py`
- Modify: `app.py`

**Interfaces:**
- Consumes: everything Task 5 produced, plus `text_to_sql_agent.evaluation` from Task 4.
- Produces: `ui.settings.Settings`; `ui.sidebar.render_sidebar() -> Settings`; `ui.chat.render_chat(settings: Settings) -> None`; `ui.evaluation.render_evaluation(settings: Settings) -> None`.

- [ ] **Step 1: Define the settings contract**

Create `ui/settings.py`:

```python
"""The settings the sidebar collects and the rest of the UI consumes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """One render's worth of user choices, passed explicitly rather than via session state.

    The sidebar previously wrote these into `st.session_state` under string keys
    that the body read back, which made the coupling invisible once the code was
    split across modules. `st.session_state` is still used for what it is for:
    values that must survive a rerun, such as chat history.
    """

    provider: str
    model_name: str
    use_rag: bool
    rag_top_k: int
    db_path: str | None
    gemini_key: str
    key_ok: bool
```

- [ ] **Step 2: Extract the sidebar**

Create `ui/sidebar.py` with `render_sidebar() -> Settings`. Move the entire
`with st.sidebar:` block from `main()` (currently `app.py:401-...`, ending where
the sidebar context manager closes) into it, unchanged, and end the function by
constructing and returning a `Settings` from the local variables the block
already computes: `provider`, `model_name`, `use_rag`, `rag_top_k`, `gemini_key`,
`key_ok`, and the `db_path` resolved by `ui.uploads.active_db_path()`.

Keep every `key="sb_*"` widget key exactly as it is. Streamlit uses those keys to
persist widget values across reruns; changing one silently resets that control.

- [ ] **Step 3: Extract the chat loop**

Create `ui/chat.py` with `render_chat(settings: Settings) -> None`. Move the chat
portion of `main()` — the history replay, `st.chat_input`, the query call and the
result rendering — unchanged, reading from `settings` instead of the local
variables it used to close over.

- [ ] **Step 4: Extract the evaluation tab**

Create `ui/evaluation.py` with `render_evaluation(settings: Settings) -> None`.
Move the evaluation expander and the `_evaluate_cases` function from
`app.py:282-347`, calling `text_to_sql_agent.evaluation.load_cases` and
`rows_match` rather than any local copy (Task 4 already removed those).

- [ ] **Step 5: Reduce `app.py`**

`app.py` becomes the page config, the header, and a `main()` that wires the three
render functions together. It must keep the name `main`, the
`if __name__ == "__main__":` guard, and the `st.set_page_config(...)` call as the
first Streamlit call on the page — Streamlit errors if anything else runs first.

- [ ] **Step 6: Confirm the size and the entrypoint**

```bash
wc -l app.py
uv run python -c "import app; print(callable(app.main))"
```

Expected: under 100 lines, and `True`.

- [ ] **Step 7: Check nothing still reads sidebar values from session state**

```bash
grep -rn 'session_state\[.sb_\|session_state.get(.sb_' app.py ui/
```

Expected: no output. Widget `key="sb_*"` declarations inside `ui/sidebar.py` are
fine and expected — this checks for *readers* elsewhere, which are what the
`Settings` dataclass replaces.

- [ ] **Step 8: Run the app and work the checklist**

The split has no automated test. Run it and check each item by hand:

```bash
uv run streamlit run app.py --server.headless true --server.port 8501
```

At `http://localhost:8501`, confirm: each of the three demo databases loads; a
CSV upload ingests and becomes queryable; asking a question returns a table; the
generated SQL is visible; the schema RAG report renders; the evaluation expander
runs and shows results; switching provider between Gemini and Ollama updates the
model list. Stop with Ctrl-C. Record in your report which items you checked and
any that you could not.

If no API key is available, use gold mode in the evaluation tab and the Ollama
provider for the chat, or mock at the module level — a missing key is not a
regression.

- [ ] **Step 9: Gates and commit**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git status --short
git add ui app.py
git commit -m "refactor(ui): split main() into sidebar, chat and evaluation modules

app.py was 759 lines with a 367-line main() covering secrets, uploads,
the sidebar, the chat loop, chart heuristics and an evaluation tab. It is
now a thin entrypoint that wires three render functions together.

The one non-mechanical change: the sidebar returned its choices through
st.session_state string keys that the body read back, coupling that goes
invisible once the code spans modules. It now returns a frozen Settings
dataclass, and session state is left to what it is for - values that must
survive a rerun, such as chat history. Widget key='sb_*' names are
unchanged, since Streamlit uses them to persist control values.

app.py keeps its name, its main() and its set_page_config call: Streamlit
Cloud runs it by name and errors if any other Streamlit call precedes the
page config.

No behaviour change and no automated test can prove that, so this lands
as its own commit for bisectability and was checked by hand against a
written checklist - see the commit's task report.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Type-check the UI

**Files:**
- Modify: `pyproject.toml`, then whatever mypy reports
- Test: `tests/test_packaging.py`

**Interfaces:**
- Consumes: the `ui/` package from Tasks 5 and 6.
- Produces: `uv run mypy` covering `app.py`, `ui/` and `scripts/`.

**Background.** `mypy` is scoped to `files = ["text_to_sql_agent"]`, so a rename
in `__init__.py`'s `__all__` would leave `backend.<old_name>` rotting in `app.py`
with every gate green and the app raising `AttributeError` on the first query.

- [ ] **Step 1: Prove the gap exists before closing it**

```bash
printf '\nbackend.this_attribute_does_not_exist\n' >> app.py
uv run ruff check app.py; uv run mypy; uv run pytest -q 2>&1 | tail -1
git checkout app.py
```

Expected: all three pass despite the broken reference. That is the gap. Confirm
`git status --short` is clean after the checkout.

- [ ] **Step 2: Widen mypy's scope**

In `pyproject.toml` under `[tool.mypy]` set:

```toml
files = ["text_to_sql_agent", "ui", "app.py", "scripts"]
```

Keep `strict = true`. Add a relaxation for the new paths:

```toml
# Streamlit's decorated API produces no-any-return and type-arg noise that
# strict mode cannot resolve without casts that document nothing. The backend
# package stays strict; the presentation layer is checked for real errors -
# above all, attribute access against the package - rather than for total
# annotation coverage.
[[tool.mypy.overrides]]
module = ["app", "ui.*", "scripts.*"]
disallow_untyped_defs = false
disallow_any_generics = false
warn_return_any = false
ignore_missing_imports = true
```

- [ ] **Step 3: Fix what it reports**

Run: `uv run mypy` and work the list. Add annotations; do not add blanket
`# type: ignore` headers, and do not silence an error that indicates a real bug —
fix the bug. If something cannot be fixed without changing behaviour, leave
`# type: ignore[<code>]  # Phase 3` with a comment and record it in
`docs/4_next_steps.md`.

- [ ] **Step 4: Prove the gap is now closed**

```bash
printf '\nbackend.this_attribute_does_not_exist\n' >> app.py
uv run mypy; echo "exit=$?"
git checkout app.py
```

Expected: mypy **fails** with `Module has no attribute` and a non-zero exit.
Confirm `git status --short` is clean afterwards.

- [ ] **Step 5: Guard the scope with a test**

Append to `tests/test_packaging.py`:

```python
def test_mypy_covers_the_presentation_layer() -> None:
    """app.py and ui/ must stay in mypy's scope.

    Narrowing this back to the package alone would let a rename in
    __all__ rot `backend.<name>` in app.py with every gate still green.
    """
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    files = pyproject["tool"]["mypy"]["files"]
    for required in ("text_to_sql_agent", "ui", "app.py"):
        assert required in files, f"mypy must check {required}"
```

- [ ] **Step 6: Gates and commit**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
git status --short
git add pyproject.toml tests/test_packaging.py app.py ui scripts
git commit -m "fix(types): extend mypy to app.py, ui/ and scripts/

mypy was scoped to files = ['text_to_sql_agent'], and ruff does not flag
bad module-attribute access, so a rename in __init__.py's __all__ would
have left backend.<old_name> rotting in app.py with ruff, mypy and pytest
all green and the app raising AttributeError on the user's first query.
Confirmed by appending a bogus backend attribute and watching all three
gates pass.

The presentation layer is checked with relaxed settings rather than
strict: Streamlit's decorated API produces no-any-return and type-arg
noise that strict mode cannot resolve without casts that document
nothing. Attribute access against the package - the thing that actually
rots - is still checked. text_to_sql_agent stays strict.

A test asserts the scope, so narrowing it back would fail CI.

Verified: the bogus-attribute probe now fails mypy with a non-zero exit.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Documentation and end-to-end verification

**Files:**
- Modify: `docs/0_coding_standards.md`, `docs/3_decisions.md`, `docs/4_next_steps.md`, `docs/6_agent_log.md`, `README.md`, `docs/2_architecture.md`, `AGENTS.md`

**Interfaces:**
- Consumes: everything.
- Produces: a verified, documented phase.

- [ ] **Step 1: Record the decisions**

Add dated entries to `docs/3_decisions.md`, newest first, each stating what was
chosen and what it ruled out: the multi-statement guard replacing the keyword
regex; sqlglot failing closed rather than open; keeping the VM-step guard and
making it a typed result rather than removing it; `ui/` beside `app.py` rather
than inside the package, and deliberately not in the wheel; relaxed mypy for the
presentation layer.

- [ ] **Step 2: Update the project standards**

In `docs/0_coding_standards.md`, add the two-tier mypy strictness and the `ui/`
placement rule to §2, and add `QUERY_ABORTED_AFTER_<n>_VM_STEPS` to the error
codes named in §3. Record only deltas — never restate the master standard.

- [ ] **Step 3: Update the roadmap docs**

In `docs/4_next_steps.md`, remove everything Phase 2 completed and leave Phases
3-5 plus anything deferred with a `Phase 3` marker. Find deferrals with
`grep -rn "Phase 3" --include="*.py" .` and say plainly if there are none.

In `AGENTS.md`, update "Current state" with the date and the new test count, and
remove from "Open risks" the `safety.py` and `execution.py` entries this phase
fixed. Leave the `rag.py` entry — that is Phase 4.

- [ ] **Step 4: Fix the architecture doc's stale claims**

`docs/2_architecture.md` describes the safety layer. Re-read it against the new
`safety.py` and correct anything about keyword filtering. Check its test count
too — Phase 1 already had to fix a stale count there.

- [ ] **Step 5: Verify every checkable number you just wrote**

```bash
uv run pytest 2>&1 | tail -1
wc -l app.py
grep -c "" docs/4_next_steps.md
```

Any figure you put in a doc must match a command you ran. Do not write a number
you have not measured — a stale count in these docs has already had to be fixed
twice in this project.

- [ ] **Step 6: Full end-to-end gate**

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -c "import app; print('app imports')"
uv run python scripts/evaluate_text_to_sql.py --mode gold 2>&1 | tail -3
```

All must pass.

- [ ] **Step 7: Write the agent log entry**

Append a dated entry to `docs/6_agent_log.md` — **append only, never edit an
existing entry**. Record what changed, what was verified with the actual commands
and outputs, what was found but not fixed, and anything still open. **Do not
write a claim you have not personally checked.** The Phase 1 log carried a
"verified" claim that had been written before the check was run, and it had to be
corrected by a later entry; do not repeat that.

- [ ] **Step 8: Commit and push**

```bash
git status --short
git add docs README.md AGENTS.md
git commit -m "docs: record Phase 2 decisions and verification

Adds dated entries for the multi-statement safety guard, sqlglot failing
closed, keeping the VM-step guard as a typed result, ui/ placement and
its deliberate absence from the wheel, and relaxed mypy for the
presentation layer.

Drops the safety.py and execution.py entries from AGENTS.md's open risks,
which this phase fixed, and corrects docs/2_architecture.md's description
of the safety layer, which still described keyword filtering.

Every figure written here was measured by a command in this commit's task
report rather than carried over.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push
```

- [ ] **Step 9: Confirm CI**

```bash
sleep 45 && gh run list --limit 1
```

Expected: the newest run `completed  success`. If `gh` is unavailable, say so
plainly rather than assuming. If any job failed, fix it before calling the phase
done.

---

## Self-Review

**Spec coverage.** §4.1 safety → Task 1. §4.2 execution → Task 2. §4.3 shared
evaluation → Task 4. §4.4 `app.py` split → Tasks 5 and 6. §4.5 mypy → Task 7.
§4.6 pipeline tests → Task 3 (plus the abort test in Task 2). §5's ordering is
followed exactly. §6's risks are each mitigated by a named step: the safety
corpus (Task 1 Steps 2 and 7), the manual UI checklist (Task 6 Step 8), the
error-code consumer update (Task 5 Steps 4-6), Streamlit Cloud (Task 6 Step 6).
§7's definition of done is Task 8 Step 6.

**Placeholder scan.** No TBDs. Every code step carries the code. The one place
full code is not reproduced is Task 6, where the instruction is explicitly to
move existing blocks unchanged — reproducing 370 lines of Streamlit layout in the
plan would invite editing during transcription, which is the opposite of what
that task needs.

**Type consistency.** `Settings` fields defined in Task 6 Step 1 are exactly what
Task 6 Steps 2-4 consume. `describe_error(code: str | None) -> str` is defined in
Task 5 Step 5 and used in Task 5 Step 6's tests with the same signature.
`rows_match`, `canonical_value`, `normalise_rows` and `load_cases` are defined in
Task 4 Step 1 and consumed under those exact names in Task 4 Step 5 and Task 6
Step 4. `max_vm_steps` and `DEFAULT_MAX_VM_STEPS` are introduced in Task 2 Steps
3-4 and used consistently thereafter; no step still references
`progress_steps` or `DEFAULT_SQLITE_PROGRESS_STEPS` except Task 2 Step 5, which
exists to find and remove them.

**Known plan-level risk.** Task 6 has no automated proof of correctness, by
design — the mitigation is move-only discipline, a separate commit, and a manual
checklist. If the reviewer of that task wants stronger evidence, the honest
answer is that a Streamlit UI test harness is Phase 3 scope, not something to
bolt on mid-split.
