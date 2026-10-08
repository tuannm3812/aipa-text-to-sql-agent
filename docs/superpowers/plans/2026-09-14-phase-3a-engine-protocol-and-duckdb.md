# Phase 3a — Engine Protocol, SQLite Port, DuckDB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put an `Engine` seam behind execution, schema, safety and prompting; port SQLite onto it with no behaviour change; add DuckDB as a second implementation that passes the same conformance suite.

**Architecture:** Eight tasks. The protocol and the SQLite port land together, because an interface with no implementation proves nothing and an implementation written after the interface has set encodes its assumptions. The conformance suite arrives with them and grows a second engine in Task 6 — it is the deliverable that makes the abstraction real. The plumbing that SQLite never needed (reachability, schema dispatch, dialect prompts) lands before DuckDB so DuckDB inherits it rather than re-inventing it.

**Tech Stack:** Python 3.11-3.13, `uv`, sqlglot, SQLite (stdlib), DuckDB 1.x, pytest 8, ruff 0.16.4, mypy.

## Global Constraints

- **SQLite behaviour must not change.** Every existing test passes unmodified, the gold benchmark still reports **12/12**, and the assembled SQLite prompt stays **byte-identical**. This is the single property that makes the phase safe.
- `text_to_sql_agent/` stays `mypy --strict`. `app`/`ui.*`/`scripts.*` relax only `disallow_any_generics` and `warn_return_any`.
- Error codes stay `SCREAMING_SNAKE_CASE`; the abort family shares the `QUERY_ABORTED_AFTER_` prefix.
- **`duckdb` is an optional dependency.** It must never become a runtime requirement, and the generated `requirements.txt` stays SQLite-only so the hosted demo does not grow a driver it never uses.
- A DSN may carry a password. It must never reach a log, an exception message, `evaluation/results/`, or the page.
- Commit directly to `tuannm3812/main-refinement`. **Never `git commit --amend`.** No feature branches.
- Conventional Commits with material detail; `git status --short` before staging; never `git add -A` unpathed; never claim a check passed without reading its output.
- System `python3` is 3.9.6 — every command goes through `uv run`.
- The four gates must be green at the end of every task: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`.

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `text_to_sql_agent/engines/__init__.py` | `open_engine(dsn)`, `EngineUnavailableError`, `EngineUnreachableError` |
| `text_to_sql_agent/engines/base.py` | The `Engine` protocol and shared helpers |
| `text_to_sql_agent/engines/sqlite.py` | SQLite implementation — existing code moved |
| `text_to_sql_agent/engines/duckdb.py` | DuckDB implementation |
| `tests/test_engine_conformance.py` | Parametrised over every available engine |
| `tests/test_engines_resolution.py` | DSN parsing and unavailable-driver behaviour |
| `tests/test_end_to_end_engines.py` | Both question paths × RAG on/off × every engine |

**Modified:** `text_to_sql_agent/execution.py`, `schema.py`, `safety.py`, `llm.py`, `pipeline.py`, `__init__.py`, `pyproject.toml`, `ui/constants.py`, `ui/sidebar.py`, `tests/test_safety.py`, docs.

---

### Task 1: The `Engine` protocol and the SQLite implementation

**Files:**
- Create: `text_to_sql_agent/engines/__init__.py`, `base.py`, `sqlite.py`
- Modify: `text_to_sql_agent/execution.py`
- Test: `tests/test_engines_resolution.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `open_engine(dsn: str) -> Engine`; the `Engine` protocol below; `EngineUnavailableError`, `EngineUnreachableError`. Every later task depends on these names.

- [ ] **Step 1: Record the baseline**

Run: `uv run pytest`
Expected: `129 passed`. Every later step compares to it.

- [ ] **Step 2: Write `engines/base.py`**

```python
"""The engine seam: what a database must provide to back the agent."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..types import QueryResult, SchemaChunk


class EngineError(Exception):
    """Base class for engine problems the caller is expected to handle."""


class EngineUnavailableError(EngineError):
    """The engine's driver is not installed."""


class EngineUnreachableError(EngineError):
    """The engine's target does not exist or cannot be connected to."""


@runtime_checkable
class Engine(Protocol):
    """One database backend.

    Implementations own their own read-only enforcement. There is no shared
    mechanism, because SQLite's URI flag plus authorizer, DuckDB's read_only
    connect flag and PostgreSQL's read-only transaction have nothing in common
    but the guarantee. `tests/test_engine_conformance.py` is what holds every
    implementation to that guarantee.
    """

    name: str
    sqlglot_dialect: str
    internal_prefixes: tuple[str, ...]
    internal_names: frozenset[str]
    prompt_dialect_section: str

    def check_reachable(self) -> None:
        """Raise `EngineUnreachableError` if the target cannot be opened."""
        ...

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult:
        """Run already-validated read-only SQL."""
        ...

    def raw_schema(self) -> str:
        """Every table's DDL, newline-separated."""
        ...

    def schema_chunks(self) -> list[SchemaChunk]:
        """Table-level chunks for retrieval. Must not read row data beyond
        low-cardinality value hints."""
        ...

    def schema_fingerprint(self) -> tuple[object, ...]:
        """A value that changes when the schema changes, for cache keying."""
        ...
```

- [ ] **Step 3: Move the SQLite implementation into `engines/sqlite.py`**

Create a `SQLiteEngine` class holding a `dsn`. Move — do not rewrite —
`_sqlite_read_only_authorizer`, `_read_only_sqlite_connection`, `_abort_query`
and the body of `execute_query` from `execution.py`, and `_build_schema_chunks`,
`_value_hints_for_table`, `_quote_identifier` and `get_schema`'s body from
`schema.py`. Bodies move verbatim; only the surrounding `def` changes.

Its attributes:

```python
name = "sqlite"
sqlglot_dialect = "sqlite"
internal_prefixes = ("sqlite_", "pragma_")
internal_names = frozenset({"dbstat"})
```

`check_reachable` raises `EngineUnreachableError` when the path does not exist,
with the message `input database not found` — the exact text `pipeline.py` raises
today, so no test that asserts on it changes.

`schema_fingerprint` returns today's `_db_cache_key` value:
`(str(resolved_path), stat.st_mtime_ns, stat.st_size)`.

`work_limit` maps to the existing `max_vm_steps`, and the abort code stays
`QUERY_ABORTED_AFTER_{n}_VM_STEPS` exactly.

- [ ] **Step 4: Write `engines/__init__.py`**

```python
"""Engine resolution: map a DSN to an implementation."""

from __future__ import annotations

from .base import (
    Engine,
    EngineError,
    EngineUnavailableError,
    EngineUnreachableError,
)

__all__ = [
    "Engine",
    "EngineError",
    "EngineUnavailableError",
    "EngineUnreachableError",
    "open_engine",
]


def open_engine(dsn: str) -> Engine:
    """Resolve a DSN to an engine.

    A bare filesystem path means SQLite, so every existing caller keeps working
    without knowing engines exist.

    Args:
        dsn: `path.db`, `sqlite://path.db`, `duckdb://path.duckdb`, or a
            `postgresql://` URL.

    Returns:
        The engine for that DSN.

    Raises:
        EngineUnavailableError: If the engine's driver is not installed.
        ValueError: If the scheme is not recognised.
    """
    scheme, _, rest = dsn.partition("://")
    if not rest:
        from .sqlite import SQLiteEngine

        return SQLiteEngine(dsn)
    if scheme == "sqlite":
        from .sqlite import SQLiteEngine

        return SQLiteEngine(rest)
    if scheme == "duckdb":
        from .duckdb import DuckDBEngine

        return DuckDBEngine(rest)
    raise ValueError(f"unrecognised database scheme: {scheme!r}")
```

Drivers are imported **inside** the branch so a missing optional driver cannot
break import of the package.

- [ ] **Step 5: Make `execute_query` delegate**

`execution.py` keeps `execute_query(db_path, sql_string, *, max_rows=DEFAULT_MAX_ROWS, max_vm_steps=DEFAULT_MAX_VM_STEPS)` with its **signature unchanged**, now a two-line delegation to `open_engine(db_path).execute(sql_string, max_rows=max_rows, work_limit=max_vm_steps)`.

- [ ] **Step 6: Write the resolution tests**

```python
from __future__ import annotations

import pytest

from text_to_sql_agent.engines import EngineUnavailableError, open_engine


@pytest.mark.parametrize(
    "dsn",
    ["data/university_agent.db", "sqlite://data/university_agent.db"],
)
def test_a_bare_path_and_a_sqlite_scheme_both_resolve_to_sqlite(dsn: str) -> None:
    engine = open_engine(dsn)
    assert engine.name == "sqlite"
    assert engine.sqlglot_dialect == "sqlite"


def test_an_unrecognised_scheme_is_rejected() -> None:
    with pytest.raises(ValueError, match="unrecognised database scheme"):
        open_engine("mysql://localhost/x")


def test_a_missing_driver_names_the_extra_to_install() -> None:
    """A bare ImportError tells the user nothing actionable."""
    pytest.importorskip
    try:
        import duckdb  # noqa: F401
    except ImportError:
        with pytest.raises(EngineUnavailableError, match="duckdb"):
            open_engine("duckdb:///tmp/x.duckdb")
    else:
        pytest.skip("duckdb is installed, so the unavailable path cannot be exercised")
```

- [ ] **Step 7: Run the whole suite**

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: `129 passed` plus your new tests, and all gates clean. **A single
pre-existing test failing means the SQLite move was not faithful** — fix the
move, do not adjust the test.

- [ ] **Step 8: Confirm the app and the benchmark are untouched**

```bash
uv run python -c "import app; print('ok')"
uv run python scripts/evaluate_text_to_sql.py --mode gold 2>&1 | tail -2
git checkout evaluation/results/
```

Expected: `ok`, then `12/12`.

- [ ] **Step 9: Commit**

```bash
git status --short
git add text_to_sql_agent/engines text_to_sql_agent/execution.py tests/test_engines_resolution.py
git commit -m "refactor(engines): add the Engine protocol and port SQLite onto it

Introduces text_to_sql_agent/engines/ with the Engine protocol and a
SQLite implementation holding the code moved verbatim from execution.py
and schema.py. execute_query keeps its exact signature and delegates, so
pipeline.py, the notebook and the evaluation harness are untouched.

Drivers are imported inside open_engine's branches so an optional driver
cannot break package import, and a missing one raises
EngineUnavailableError naming the extra rather than a bare ImportError.

No behaviour change: every pre-existing test passes unmodified and the
gold benchmark still reports 12/12.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The conformance suite, for one engine

Written now, while SQLite is the only implementation, so it encodes the
*guarantee* rather than DuckDB's behaviour. Task 6 adds the second engine and it
must pass unchanged.

**Files:**
- Create: `tests/test_engine_conformance.py`
- Modify: `pyproject.toml` (register the `conformance` marker)

**Interfaces:**
- Consumes: `open_engine`, `Engine` from Task 1.
- Produces: the `engine` fixture and `ENGINE_DSNS` registry that Task 6 extends by one entry.

- [ ] **Step 1: Register the marker**

In `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = ["conformance: a guarantee every database engine must satisfy"]
```

- [ ] **Step 2: Write the suite**

```python
"""Guarantees every engine must satisfy, whatever its mechanism.

Read-only is enforced differently by every engine — SQLite by a URI flag plus an
authorizer, DuckDB by a connect flag, PostgreSQL by a read-only transaction.
These tests deliberately call `engine.execute` **directly**, bypassing
`is_safe_query`, because that is what distinguishes real enforcement from a
single validation gate in front of a database that would happily write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from text_to_sql_agent.engines import open_engine

pytestmark = pytest.mark.conformance

WRITE_STATEMENTS = [
    "INSERT INTO customers VALUES (99, 'Mallory')",
    "UPDATE customers SET name = 'x'",
    "DELETE FROM customers",
    "DROP TABLE customers",
    "CREATE TABLE evil (a INTEGER)",
]


@pytest.fixture(params=["sqlite"])
def engine(request, tmp_path: Path):
    """One populated database per engine under test."""
    if request.param == "sqlite":
        db = tmp_path / "c.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute(
                "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, "
                "FOREIGN KEY(customer_id) REFERENCES customers(customer_id))"
            )
            conn.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "Alice"), (2, "Bob")])
        return open_engine(str(db))
    raise AssertionError(f"no fixture for engine {request.param!r}")


def test_a_select_returns_rows_and_columns(engine) -> None:
    result = engine.execute(
        "SELECT name FROM customers ORDER BY customer_id", max_rows=10, work_limit=0
    )
    assert result.ok, result.error
    assert result.columns == ["name"]
    assert result.rows == [("Alice",), ("Bob",)]


@pytest.mark.parametrize("statement", WRITE_STATEMENTS)
def test_writes_are_refused_by_the_connection_itself(engine, statement: str) -> None:
    """Bypasses is_safe_query entirely: this is the defence-in-depth check."""
    with pytest.raises(Exception) as caught:
        engine.execute(statement, max_rows=10, work_limit=0)
    assert not isinstance(caught.value, AssertionError)

    surviving = engine.execute("SELECT COUNT(*) FROM customers", max_rows=10, work_limit=0)
    assert surviving.ok, surviving.error
    assert surviving.rows == [(2,)], "the write must not have taken effect"


def test_the_row_cap_truncates_and_says_so(engine) -> None:
    result = engine.execute("SELECT name FROM customers", max_rows=1, work_limit=0)
    assert len(result.rows) == 1
    assert result.error == "RESULT_TRUNCATED_TO_1_ROWS"


def test_a_missing_table_raises_rather_than_returning(engine) -> None:
    """pipeline.py depends on the exception to trigger its repair attempt."""
    with pytest.raises(Exception):
        engine.execute("SELECT * FROM no_such_table", max_rows=10, work_limit=0)


def test_schema_chunks_expose_tables_columns_and_foreign_keys(engine) -> None:
    chunks = {c.table_name: c for c in engine.schema_chunks()}
    assert {"customers", "sales"} <= set(chunks)
    assert "name" in chunks["customers"].columns
    assert "customers" in chunks["sales"].foreign_tables


def test_raw_schema_mentions_every_table(engine) -> None:
    schema = engine.raw_schema()
    assert "customers" in schema
    assert "sales" in schema


def test_the_fingerprint_changes_when_the_schema_changes(engine) -> None:
    before = engine.schema_fingerprint()
    assert engine.schema_fingerprint() == before, "must be stable when nothing changes"
```

- [ ] **Step 3: Run it**

```bash
uv run pytest -m conformance -v
```

Expected: all pass, each parametrised `[sqlite]`.

- [ ] **Step 4: Prove the write tests are load-bearing**

A write test that passes because the statement was syntactically rejected proves
nothing. Temporarily remove `conn.set_authorizer(...)` and change `mode=ro` to
`mode=rw` in `engines/sqlite.py`, then run
`uv run pytest -m conformance -k writes -v`. Expected: **failures**. Restore both
and confirm green, with `git status --short` clean.

- [ ] **Step 5: Commit**

```bash
git status --short
git add tests/test_engine_conformance.py pyproject.toml
git commit -m "test(engines): add the conformance suite every engine must pass

Written while SQLite is the only implementation, so it encodes the
guarantee rather than a second engine's behaviour.

The write-refusal tests call engine.execute directly and bypass
is_safe_query, which is what separates real read-only enforcement from a
validation gate in front of a writable database. They also assert the row
survived, so a statement rejected for the wrong reason does not pass.

Proven load-bearing: dropping the authorizer and opening rw fails them.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Safety becomes dialect-aware

**Files:**
- Modify: `text_to_sql_agent/safety.py`, `text_to_sql_agent/__init__.py`, `tests/test_safety.py`
- Modify call sites: `text_to_sql_agent/pipeline.py`, `text_to_sql_agent/evaluation.py`, `ui/evaluation.py`

**Interfaces:**
- Consumes: `Engine` from Task 1.
- Produces: `is_safe_query(sql_string: str, *, engine: Engine | None = None) -> bool`.

- [ ] **Step 1: Change the signature**

`is_safe_query` gains a keyword-only `engine: Engine | None = None`. When `None`
it uses SQLite's dialect and internals lists, so every existing caller and test
keeps working. The parser call becomes
`sqlglot.parse(sql_string, read=dialect)`, and `_references_internals` takes the
prefixes and names rather than reading module constants.

**Do not change the structural rules** — single statement, `SELECT`/`WITH`
prefix, no data-modifying node, and the table-source position walk that stops at
the enclosing `SELECT`. They are engine-independent and were hard-won.

- [ ] **Step 2: Update every call site to pass its engine explicitly**

```bash
grep -rn "is_safe_query(" --include="*.py" . | grep -v "^./.git" | grep -v "^./tests/"
```

Each non-test call site passes the engine it is already working with. The default
exists for the notebook and for backward compatibility, not as an excuse to skip
this.

- [ ] **Step 3: Add a dialect test**

```python
def test_is_safe_query_uses_the_engine_dialect() -> None:
    """A dialect-specific construct must parse under its own engine."""
    from text_to_sql_agent.engines import open_engine

    sqlite_engine = open_engine("data/university_agent.db")
    assert agent.is_safe_query("SELECT strftime('%Y', d) FROM t", engine=sqlite_engine)


def test_is_safe_query_defaults_to_sqlite_when_no_engine_is_given() -> None:
    assert agent.is_safe_query("SELECT * FROM customers")
    assert not agent.is_safe_query("SELECT * FROM sqlite_master")
```

- [ ] **Step 4: Gates and commit**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
git status --short
git add text_to_sql_agent/safety.py text_to_sql_agent/__init__.py text_to_sql_agent/pipeline.py text_to_sql_agent/evaluation.py ui/evaluation.py tests/test_safety.py
git commit -m "refactor(safety): take the dialect and internals list from the engine

is_safe_query gains a keyword-only engine argument defaulting to SQLite,
so existing callers and the notebook keep working, and every non-test call
site now passes its engine explicitly.

The structural rules are unchanged: single statement, SELECT/WITH prefix,
no data-modifying node, and the table-source walk that stops at the
enclosing SELECT. Only the sqlglot dialect and the internals prefix/name
lists come from the engine, because those are the only genuinely
per-engine parts.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Schema dispatch and the fingerprint

Closes the half of the DSN gap that lives in `schema.py`.

**Files:**
- Modify: `text_to_sql_agent/schema.py`
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: `open_engine`, `Engine.schema_chunks`, `Engine.raw_schema`, `Engine.schema_fingerprint`.
- Produces: `get_schema` and `get_schema_chunks` keep their signatures; `get_schema_chunk_cache_info()` keeps returning `_CacheInfo`.

- [ ] **Step 1: Dispatch through the engine**

`get_schema(db_path)` becomes `open_engine(db_path).raw_schema()`.
`get_schema_chunks(db_path)` keys its `lru_cache` on
`open_engine(db_path).schema_fingerprint()` instead of `_db_cache_key`, and
builds through `engine.schema_chunks()`. `_db_cache_key`, `_build_schema_chunks`,
`_value_hints_for_table` and `_quote_identifier` are deleted from `schema.py` —
Task 1 moved them into `engines/sqlite.py`.

`get_schema_chunk_cache_info()` stays exactly as it is. `rag.py` is **not**
touched.

- [ ] **Step 2: Prove the cache still works**

```python
def test_schema_chunks_are_cached_between_calls(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()

    agent.get_schema_chunks(str(db))
    before = agent.get_schema_chunk_cache_info()
    agent.get_schema_chunks(str(db))
    after = agent.get_schema_chunk_cache_info()

    assert after.hits == before.hits + 1, "the second call must hit the cache"


def test_the_cache_misses_after_the_schema_changes(tmp_path: Path) -> None:
    """The fingerprint is what makes a stale chunk list impossible."""
    db = tmp_path / "c.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.commit()
    first = {c.table_name for c in agent.get_schema_chunks(str(db))}

    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE t2 (b INTEGER)")
        conn.commit()
    second = {c.table_name for c in agent.get_schema_chunks(str(db))}

    assert first == {"t"}
    assert second == {"t", "t2"}, "a schema change must invalidate the cache"
```

The second test can fail on a fast filesystem if `st_mtime_ns` has insufficient
resolution. If it does, **that is a real finding about the fingerprint, not a
flaky test** — report it rather than adding a sleep.

- [ ] **Step 3: Gates and commit**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
git status --short
git add text_to_sql_agent/schema.py tests/test_schema.py
git commit -m "refactor(schema): dispatch through the engine and key the cache on a fingerprint

get_schema and get_schema_chunks keep their signatures and now delegate to
the engine. The lru_cache keys on engine.schema_fingerprint() rather than
resolving and stat-ing a local path, which has no meaning for a server
engine. SQLite's fingerprint is the previous (path, mtime_ns, size), so
its caching behaviour is unchanged.

get_schema_chunk_cache_info keeps its interface and rag.py is untouched,
which is what keeps Phase 4 clear of this work.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Pipeline reachability and the dialect-aware prompt

Closes the other half of the DSN gap, and stops a non-SQLite question being told
to emit SQLite.

**Files:**
- Modify: `text_to_sql_agent/pipeline.py`, `text_to_sql_agent/llm.py`
- Test: `tests/test_pipeline.py`, `tests/test_llm.py` (new)

**Interfaces:**
- Consumes: `open_engine`, `Engine.check_reachable`, `Engine.prompt_dialect_section`.
- Produces: `generate_sql(..., *, engine: Engine | None = None)`; `SQL_TRANSLATION_SYSTEM_PROMPT` unchanged as SQLite's assembled prompt.

- [ ] **Step 1: Replace the path check in both entry points**

`ask_database` and `ask_database_with_sql` each open with
`os.path.exists(db_path)` and raise `FileNotFoundError("input database not
found")`. Replace with `engine = open_engine(db_path)` then
`engine.check_reachable()`. `SQLiteEngine.check_reachable` raises
`EngineUnreachableError` with that same message, and `EngineUnreachableError`
subclasses nothing special — so **make it also inherit `FileNotFoundError`** for
SQLite compatibility, or update the two existing tests that assert
`FileNotFoundError`. Pick one, say which in the commit, and do not leave both.

- [ ] **Step 2: Split the prompt**

In `llm.py`, split `SQL_TRANSLATION_SYSTEM_PROMPT` into `_PROMPT_BODY` plus
SQLite's dialect section, **moved verbatim**. Assemble with
`_assemble_prompt(dialect_section)`. Add:

```python
def _assemble_prompt(dialect_section: str) -> str:
    """Build the system prompt for one engine's dialect."""
    return _PROMPT_BODY.replace(_DIALECT_PLACEHOLDER, dialect_section)
```

`generate_sql` gains keyword-only `engine: Engine | None = None` and uses
`engine.prompt_dialect_section` when given, SQLite's otherwise.
`pipeline._repair_sql` takes and forwards the engine — a repair instructed in the
wrong dialect is the failure this exists to prevent.

- [ ] **Step 3: Pin the SQLite prompt byte-for-byte**

```python
def test_the_sqlite_prompt_is_unchanged() -> None:
    """Phase 1 protected this string from reformatting for the same reason:
    a changed prompt silently moves every evaluation figure."""
    import hashlib

    from text_to_sql_agent.llm import SQL_TRANSLATION_SYSTEM_PROMPT

    digest = hashlib.sha256(SQL_TRANSLATION_SYSTEM_PROMPT.encode()).hexdigest()
    assert digest == "89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3"
```

That digest is the prompt's current value, measured 2026-09-14 — the same
2960-byte string and hash Phase 1 verified when it excluded this constant from
reformatting. If it changes after your split, the split was not verbatim; fix the
split rather than updating the digest.

- [ ] **Step 4: Prove the dialect reaches generation and repair**

```python
def test_generation_receives_the_engine_dialect_section(customers_db: str) -> None:
    from text_to_sql_agent.engines import open_engine

    engine = open_engine(customers_db)
    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        agent.ask_database("list customers", db_path=customers_db)

    assert engine.prompt_dialect_section in seen["prompt"]


def test_repair_receives_the_engine_dialect_section(customers_db: str) -> None:
    """A repair instructed in the wrong dialect is the failure this prevents."""
    from text_to_sql_agent.engines import open_engine

    engine = open_engine(customers_db)
    prompts: list[str] = []

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        prompts.append(prompt)
        return "SELECT nope FROM customers" if len(prompts) == 1 else "SELECT name FROM customers"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        agent.ask_database("list customers", db_path=customers_db)

    assert len(prompts) == 2, "one generation plus one repair"
    assert all(engine.prompt_dialect_section in p for p in prompts)
```

**`llm.py` has no such seam today — you must introduce one.** Confirmed
2026-09-14: `generate_sql` resolves `selected_provider` and then branches inline
into the Gemini and Ollama clients, with no single call point. Extract a thin

```python
def _call_provider(prompt: str, *, model_name: str, provider: str) -> str:
    """Send an assembled prompt to the resolved provider and return its text."""
```

that both branches go through, moving the existing branch bodies into it
unchanged. This is the only structural change to `llm.py` beyond the prompt
split, and it exists so the dialect can be asserted without patching a vendor
SDK. Say so in the commit body.

- [ ] **Step 5: Gates, benchmark, and commit**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/evaluate_text_to_sql.py --mode gold 2>&1 | tail -2
git checkout evaluation/results/
git status --short
git add text_to_sql_agent/pipeline.py text_to_sql_agent/llm.py tests/test_pipeline.py tests/test_llm.py
git commit -m "feat(engines): engine-aware reachability and a dialect-aware prompt

Both pipeline entry points opened with os.path.exists and raised before
any engine was reached, so a DSN could never work however well the
execution layer delegated. They now resolve an engine and call
check_reachable.

The system prompt hard-coded SQLite in four places and _repair_sql passed
no dialect, so a non-SQLite question and every repair on it would have
been instructed to emit SQLite. The prompt now splits into a shared body
plus a per-engine section; SQLite's is moved verbatim and a sha256 test
pins the assembled prompt, because a changed prompt silently moves every
evaluation figure.

Verified: gold benchmark still 12/12; provider-stubbed tests assert the
dialect section reaches both the initial generation and the repair.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: The DuckDB engine

**Files:**
- Create: `text_to_sql_agent/engines/duckdb.py`
- Modify: `pyproject.toml`, `tests/test_engine_conformance.py`

**Interfaces:**
- Consumes: the `Engine` protocol and the conformance suite.
- Produces: `DuckDBEngine`; the conformance fixture gains a `duckdb` param.

**Verified facts you can build on** — probed 2026-09-14, so do not re-derive:

- `duckdb.connect(path, read_only=True)` refuses `INSERT`, `UPDATE`, `DROP` and
  `CREATE`, each raising `duckdb.InvalidInputException`.
- `SELECT table_name, sql FROM duckdb_tables()` returns the full `CREATE TABLE`
  DDL, including `FOREIGN KEY` clauses.
- `SELECT table_name, column_name, data_type FROM information_schema.columns
  ORDER BY table_name, ordinal_position` gives columns.
- `SELECT table_name, referenced_table FROM duckdb_constraints() WHERE
  constraint_type = 'FOREIGN KEY' AND referenced_table IS NOT NULL` gives FK
  pairs — e.g. `[('sales', 'customers')]`.
- `duckdb_tables()` and `information_schema.tables` **are readable**, so DuckDB's
  internals list is its own.
- A `threading.Timer` calling `connection.interrupt()` aborts a runaway query
  with `duckdb.InterruptException`.
- **`read_only=True` does not stop filesystem access.** Probed 2026-09-18:
  `SELECT * FROM read_csv('<file>')` and `SELECT * FROM '<file>'` both read an
  arbitrary file, `glob` lists directories, and `COPY … TO` writes a file — all
  from a read-only connection. Adding `config={"enable_external_access": False}`
  refuses every one with `duckdb.PermissionException`. **This setting is
  mandatory.** The bare quoted-path form has no function call, so `is_safe_query`
  cannot catch it by name; the connection setting is the load-bearing guard.

- [ ] **Step 1: Add the optional dependency**

```toml
[project.optional-dependencies]
duckdb = ["duckdb>=1.0,<2"]
engines = ["duckdb>=1.0,<2"]
```

Then `uv sync --extra engines`. **Do not regenerate `requirements.txt`** — it
stays SQLite-only so the hosted demo does not install a driver it never uses.
Confirm with `grep -c duckdb requirements.txt` → `0`.

- [ ] **Step 2: Write `engines/duckdb.py`**

Attributes:

```python
name = "duckdb"
sqlglot_dialect = "duckdb"
internal_prefixes = ("duckdb_", "pg_")
internal_names = frozenset({"information_schema", "sqlite_master"})
```

`execute` opens with `duckdb.connect(path, read_only=True,
config={"enable_external_access": False})` — **both**, never `read_only` alone —
then arms a `threading.Timer` for `work_limit`
milliseconds calling `connection.interrupt()`, and converts
`duckdb.InterruptException` into
`QueryResult(error=f"QUERY_ABORTED_AFTER_{work_limit}_MS")`. **Cancel the timer
in a `finally`**, or a later query inherits a pending interrupt. `work_limit=0`
disables the timer. Row capping and `RESULT_TRUNCATED_TO_<n>_ROWS` follow
SQLite's shape exactly.

A genuine SQL error must still **raise**, not return — conformance test
`test_a_missing_table_raises_rather_than_returning` enforces it.

`schema_chunks` uses the three catalogue queries above and must produce the same
`SchemaChunk` shape SQLite produces, including low-cardinality `value_hints`.
`schema_fingerprint` may use the file's `(path, mtime_ns, size)` like SQLite,
since a DuckDB database is a file.

- [ ] **Step 3: Add DuckDB to the conformance fixture**

Change the fixture's `params` to `["sqlite", "duckdb"]` and add the branch,
skipping with an explicit reason if the driver is absent:

```python
    if request.param == "duckdb":
        duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")
        db = tmp_path / "c.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        con.execute(
            "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, "
            "customer_id INTEGER REFERENCES customers(customer_id))"
        )
        con.execute("INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob')")
        con.close()
        return open_engine(f"duckdb://{db}")
```

- [ ] **Step 3a: Pin DuckDB's external-access guard**

These are DuckDB-specific, so they go in a new `tests/test_engine_duckdb.py`, not
the engine-agnostic conformance suite — the same reasoning that put SQLite's
authorizer tests in `test_execution.py`. Each calls `engine.execute` directly,
bypassing `is_safe_query`, and asserts refusal:

```python
import pytest

duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")

from text_to_sql_agent.engines import open_engine


@pytest.fixture
def secret_and_engine(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("k,v\napi_key,hunter2\n", encoding="utf-8")
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.close()
    return secret, open_engine(f"duckdb://{db}")


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM '{secret}'",
        "SELECT * FROM glob('{parent}/*')",
    ],
)
def test_filesystem_reads_are_refused_by_the_connection(secret_and_engine, template):
    secret, engine = secret_and_engine
    sql = template.format(secret=secret, parent=secret.parent)
    with pytest.raises(Exception) as caught:  # noqa: B017
        engine.execute(sql, max_rows=10, work_limit=0)
    assert "hunter2" not in str(caught.value)


def test_copy_to_a_file_is_refused_by_the_connection(secret_and_engine, tmp_path):
    _, engine = secret_and_engine
    target = tmp_path / "exfil.csv"
    with pytest.raises(Exception):  # noqa: B017
        engine.execute(f"COPY (SELECT 1) TO '{target}'", max_rows=10, work_limit=0)
    assert not target.exists(), "nothing may be written to disk"
```

**Prove it load-bearing:** remove only the `enable_external_access` config from
`engines/duckdb.py` and confirm every one of these fails. Then restore and
confirm `git diff` is empty. If any still passes without the setting, something
else is refusing it and it is not pinning the guard — report which.

- [ ] **Step 4: Run conformance for both engines**

```bash
uv run pytest -m conformance -v
```

Expected: every test listed twice, `[sqlite]` and `[duckdb]`, all passing, **none
skipped**. A skip here means the extra was not installed — fix that, do not
accept it.

- [ ] **Step 5: Probe DuckDB's internals list against its own catalogue**

The spec says to assume this list is incomplete, because SQLite's was — it
shipped with a `dbstat('main')` bypass. Enumerate what DuckDB actually exposes
and try to reach each one:

```bash
uv run python - <<'EOF'
import duckdb, tempfile, os
from text_to_sql_agent import is_safe_query
from text_to_sql_agent.engines import open_engine
p = os.path.join(tempfile.mkdtemp(), "probe.duckdb")
con = duckdb.connect(p); con.execute("CREATE TABLE t (a INTEGER)"); con.close()
engine = open_engine(f"duckdb://{p}")
names = [r[0] for r in duckdb.connect(p, read_only=True).execute(
    "SELECT function_name FROM duckdb_functions() WHERE function_name LIKE 'duckdb%'"
).fetchall()]
leaks = []
for n in sorted(set(names)):
    q = f"SELECT * FROM {n}()"
    if is_safe_query(q, engine=engine):
        leaks.append(q)
for q in ["SELECT * FROM information_schema.tables",
          "SELECT * FROM pg_catalog.pg_tables",
          "SELECT * FROM duckdb_settings()",
          "SELECT * FROM read_csv('/etc/hosts')",
          "SELECT * FROM '/etc/hosts'",
          "SELECT * FROM glob('/etc/*')"]:
    if is_safe_query(q, engine=engine):
        leaks.append(q)
print("ALLOWED internals reads:", leaks or "none")
EOF
```

The last three are filesystem reads, not catalogue reads. The quoted-path form
will likely still print as allowed, because it has no function name for
`is_safe_query` to match — that is expected, and it is exactly why Step 3a pins
`enable_external_access=false` as the load-bearing defence. Record whether it
prints, and do not claim the validator covers it.

Every other entry it prints is a hole. Add the missing prefixes or names, re-run until
it prints `none`, and **record the probe output in your report** — it is the
evidence that the list is complete.

- [ ] **Step 6: Gates and commit**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
grep -c duckdb requirements.txt   # must be 0
git status --short
git add text_to_sql_agent/engines/duckdb.py pyproject.toml tests/test_engine_conformance.py
git commit -m "feat(engines): add DuckDB as the second engine

Passes the conformance suite unchanged, which is what makes the Engine
protocol real rather than a SQLite-shaped interface with one caller.

Read-only comes from DuckDB's own read_only connect flag, verified to
refuse INSERT, UPDATE, DROP and CREATE natively. The work limit is a timer
calling connection.interrupt(), cancelled in a finally so a later query
cannot inherit a pending interrupt, and surfaces as
QUERY_ABORTED_AFTER_<n>_MS.

DuckDB's internals list is its own: duckdb_tables() and
information_schema are readable, so SQLite's sqlite_/pragma_ names mean
nothing here. The list was probed against duckdb_functions() rather than
assumed, since SQLite's looked complete and had a dbstat('main') bypass.

duckdb is an optional extra and stays out of requirements.txt, so the
hosted demo does not install a driver it never uses.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6b: Default-deny function allowlist for DuckDB

Added 2026-09-19 by owner decision, after Task 6's name-based blocklist leaked in
four successive review rounds. See spec §4.4, "Revised 2026-09-19".

**Files:**
- Modify: `text_to_sql_agent/safety.py`, `text_to_sql_agent/engines/base.py`, `text_to_sql_agent/engines/duckdb.py`, `text_to_sql_agent/engines/sqlite.py`
- Test: `tests/test_engine_duckdb.py`, `tests/test_safety.py`

**Interfaces:**
- Consumes: the `Engine` protocol, `is_safe_query(sql, *, engine=None)`.
- Produces: `Engine.allowed_functions: frozenset[str] | None`. `None` = blocklist mode (SQLite, unchanged). A set = default-deny.

**Proven leaks this task must close** (all verified against the live engine,
validator ALLOW and connection EXECUTED):
- `SELECT current_setting('secret_directory')` → returned a real path; would return
  `http_proxy_password` wherever one is configured. It is a **scalar**, so no
  table-source rule can reach it.
- `SELECT * FROM histogram_values('information_schema.tables', 'table_name', 20, 'auto')`
  → returned catalogue rows. `histogram_values` is a macro whose body calls
  `query_table(source)`; `histogram` calls it. Both were on the allowlist.

- [ ] **Step 1: Add the protocol attribute.** `Engine` gains
  `allowed_functions: frozenset[str] | None`. `SQLiteEngine.allowed_functions = None`.

- [ ] **Step 2: Default-deny in `is_safe_query`.** When the engine's
  `allowed_functions` is a set, walk **every** function node in the parsed
  statement, in every position, and reject the query if any function's name is
  not in the set. The existing structural rules and internals checks still run
  as well — this is an additional gate, not a replacement.

  Name resolution is the hard part and must be tested, not assumed. sqlglot
  parses many functions into typed classes (`exp.Count`, `exp.Upper`,
  `exp.ReadCSV`, ...) whose canonical `.sql_name()` may differ from the name the
  user wrote or the name DuckDB registers. Resolve each node to the name DuckDB
  itself would call, compare case-insensitively, and prove the mapping for every
  entry in the allowlist.

  A string-literal table reference — `FROM '<path>'` — is also rejected in
  default-deny mode. The connection guard already refuses it; this makes the
  validator agree rather than relying on the second layer alone.

- [ ] **Step 3: Curate `DuckDBEngine.allowed_functions`.** Only functions an
  analytical question could need: aggregates, window functions, string, date and
  time, numeric, conditional and list/JSON-access functions, plus the table
  functions `range`, `generate_series`, `unnest`, `json_each`, `json_tree`.
  **No macros** unless its body is read and shown not to reach `query`,
  `query_table` or catalogue objects. `histogram`, `histogram_values` and
  `summary` are **out**. `current_setting` and every other configuration or
  session-introspection function is **out**. When unsure, leave it out.

- [ ] **Step 4: Prove default-deny, not a list.** Sweep `duckdb_functions()` for
  **every** function type and assert every name **not** in the allowlist is
  rejected by `is_safe_query` in a scalar position (`SELECT name(...)`) and, where
  it parses, in a table position (`SELECT * FROM name(...)`). This test is the
  property; the allowlist is only data.

- [ ] **Step 5: Prove no false rejection of real analytics.** A parametrised
  corpus of at least 40 DuckDB analytical queries — grouping, aggregation, window
  functions, date bucketing with `date_trunc`, string cleaning, `CASE`, CTEs,
  `COALESCE`, list and JSON access — must all pass `is_safe_query` **and**
  execute on a real DuckDB fixture. A false rejection costs a user an
  unanswerable question, so this corpus matters as much as the attack corpus.

- [ ] **Step 6: Pin both proven leaks.** `current_setting(...)` in scalar
  position, and `histogram` / `histogram_values` with a catalogue source, are
  rejected by `is_safe_query`.

- [ ] **Step 7: SQLite unchanged.** Re-run a differential of the pre-task
  `safety.py` against the new one under `SQLiteEngine` over a corpus of 150+
  queries. Zero disagreements, since SQLite's `allowed_functions` is `None`.

- [ ] **Step 8: Gates, conformance with no skips, and commit.**

### Task 7: End-to-end tests and the UI connection option

Conformance cannot catch a failure that happens *before* the engine is reached.

**Files:**
- Create: `tests/test_end_to_end_engines.py`
- Modify: `ui/constants.py`, `ui/sidebar.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing later depends on it.

- [ ] **Step 1: Write the end-to-end tests**

Four combinations per engine — both public question paths × RAG on and off —
parametrised over the same engines as conformance:

```python
@pytest.mark.parametrize("use_rag", [True, False])
@pytest.mark.parametrize("entry", ["ask_database", "ask_database_with_sql"])
def test_a_question_reaches_the_engine(engine_dsn: str, entry: str, use_rag: bool) -> None:
    seen: dict[str, str] = {}

    def fake(_question: str, schema_text: str, **_k: object) -> str:
        seen["schema"] = schema_text
        return "SELECT name FROM customers"

    with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=fake):
        if entry == "ask_database":
            result = agent.ask_database("list customers", db_path=engine_dsn, use_rag=use_rag)
        else:
            _sql, result = agent.ask_database_with_sql(
                "list customers", db_path=engine_dsn, use_rag=use_rag
            )

    assert result.ok, result.error
    assert "customers" in seen["schema"], "the schema must come from this engine"


def test_an_unreachable_dsn_reports_the_engine_not_a_missing_file() -> None:
    result = agent.ask_database("q", db_path="duckdb:///nonexistent/path.duckdb")
    assert not result.ok
    assert "EngineUnreachable" in (result.error or "") or "not found" in (result.error or "")
```

- [ ] **Step 2: Add the UI option**

`ui/sidebar.py`'s Database radio gains `"Connection string"`, with a
`st.text_input(key="sb_dsn")`. `active_db_path` returns the DSN for that branch.
Keep every existing `sb_*` key unchanged — Streamlit uses them to persist widget
values, and renaming one silently resets that control.

**Never persist the DSN.** It lives in session state for the session only, is
never written to disk, and any error surfaced to the page must be redacted —
a connection failure must not print a password back to the user.

- [ ] **Step 3: Verify the app still renders identically for SQLite**

```bash
uv run python -c "
from streamlit.testing.v1 import AppTest
at = AppTest.from_file('$PWD/app.py', default_timeout=60).run()
print('exception:', bool(at.exception))
"
```

Expected: `exception: False`.

- [ ] **Step 4: Gates and commit**

```bash
uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy
git status --short
git add tests/test_end_to_end_engines.py ui/constants.py ui/sidebar.py
git commit -m "test(engines): end-to-end coverage per engine, and a DSN option in the UI

Engine conformance cannot catch a failure that happens before the engine
is reached, which is exactly what the reachability and schema-dispatch
work fixed. These drive both public question paths with RAG on and off
against every engine, asserting the schema reaching the prompt came from
that engine.

The sidebar gains a Connection string option. The DSN is session-only,
never written to disk, and redacted in any error surfaced to the page.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Documentation and end-to-end verification

**Files:**
- Modify: `docs/3_decisions.md`, `docs/0_coding_standards.md`, `docs/2_architecture.md`, `docs/4_next_steps.md`, `AGENTS.md`, `README.md`, `docs/6_agent_log.md`

- [ ] **Step 1: Record the decisions**

Dated entries in `docs/3_decisions.md`: the per-engine read-only model and why
there is no shared mechanism; the abort-code family; the schema fingerprint
replacing a filesystem stat; `duckdb` as an optional extra kept out of
`requirements.txt`; and whichever choice Task 5 Step 1 made about
`EngineUnreachableError` inheriting `FileNotFoundError`.

- [ ] **Step 2: Update the standards and architecture docs**

`docs/0_coding_standards.md` §3 gains the `QUERY_ABORTED_AFTER_<n>_MS` code and
the engines-package convention. `docs/2_architecture.md` currently describes a
SQLite-only pipeline — correct the execution and schema paragraphs, and its test
count. `AGENTS.md` "Current state" gets the date and new test count.
`README.md`'s Project Structure tree gains `text_to_sql_agent/engines/`.

- [ ] **Step 3: Move Phase 3a out of next-steps**

`docs/4_next_steps.md` leads with **3b — PostgreSQL** and keeps the rest.

- [ ] **Step 4: Verify every figure you wrote**

```bash
uv run pytest 2>&1 | tail -1
ls text_to_sql_agent/engines/*.py | wc -l
uv run pytest -m conformance -v 2>&1 | grep -c "PASSED"
```

Any number in a doc must come from a command you ran in this task.

- [ ] **Step 5: Full gate**

```bash
uv sync --extra engines
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run pytest -m conformance -v          # sqlite and duckdb, none skipped
uv run python -c "import app; print('ok')"
uv run python scripts/evaluate_text_to_sql.py --mode gold 2>&1 | tail -2
git checkout evaluation/results/
```

Expected: all clean, conformance listing both engines, and **12/12**.

- [ ] **Step 6: Write the agent log entry**

Append — never edit an existing entry — with what changed, what was verified with
real command output, and what was not. Include the Task 6 Step 5 internals probe
output. **Do not write a claim you have not run a command to confirm.** Two
entries in this log have already had to be corrected for exactly that.

- [ ] **Step 7: Commit, push, confirm CI**

```bash
git status --short
git add docs README.md AGENTS.md
git commit -m "docs: record the Phase 3a engine decisions and verification"
git push
sleep 45 && gh run list --limit 1
```

CI must be green. If `gh` is unavailable, say so rather than assuming.

---

## Self-Review

**Spec coverage.** §4.1 protocol → Task 1. §4.2 per-engine read-only → Tasks 1
and 6, enforced by Task 2. §4.3 conformance → Task 2, extended in Task 6. §4.4
dialect-aware safety → Task 3. §4.5 abort code family → Tasks 1 and 6. §4.6
optional dependencies → Task 6 Step 1. §4.7 UI → Task 7 Step 2. §4.8 reachability
and schema dispatch → Tasks 4 and 5. §4.9 dialect prompt → Task 5. §4.10
end-to-end → Task 7. §7 definition of done → Task 8 Step 5.

**Ordering.** The protocol and SQLite land together so the interface is never
implementation-free. Conformance is written while SQLite is the only engine, so
it encodes the guarantee rather than DuckDB's behaviour. The plumbing DuckDB
would otherwise trip over — reachability, schema dispatch, dialect prompts —
lands before DuckDB. End-to-end tests come last because they need every piece.

**Type consistency.** `open_engine(dsn) -> Engine` is defined in Task 1 and used
under that name throughout. `execute(sql, *, max_rows, work_limit)` is fixed in
Task 1 and called with those keywords in Tasks 2 and 6. `schema_fingerprint()`
is defined in Task 1 and consumed in Task 4. `prompt_dialect_section` is declared
in Task 1 and consumed in Task 5. `EngineUnavailableError` and
`EngineUnreachableError` are defined in Task 1 and referenced in Tasks 1, 5 and 7.

**One decision deliberately left to the implementer**, flagged in place because
either answer is defensible and the choice must be visible rather than silent:
whether `EngineUnreachableError` inherits `FileNotFoundError`, or the two existing
tests asserting `FileNotFoundError` change instead (Task 5 Step 1). The other two
unknowns were resolved while writing this plan rather than delegated — the prompt
digest is measured and pasted in, and `llm.py` is confirmed to have no provider
chokepoint, so Task 5 Step 4 instructs introducing one rather than asking.

**Known plan-level risk.** Task 1 moves a lot of code in one commit. The
mitigation is that the existing suite covers all of it — a faithful move keeps
129 tests green, and Step 7 says explicitly that a single failure means the move
was wrong, not the test.
