# Phase 3b — PostgreSQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PostgreSQL as the third `Engine`, proving read-only its own way, and carry
schema-qualified table identity through all three engines so a database's non-default schemas stop
being invisible.

**Architecture:** `text_to_sql_agent/engines/postgres.py` implements the same `Engine` protocol
SQLite and DuckDB already satisfy (`engines/base.py`), resolved from a `postgresql://` DSN by
`open_engine`. Read-only is proven twice over — a least-privilege role *and* a read-only
transaction per statement — and validated by the same `tests/test_engine_conformance.py` suite,
parametrised over a third engine. PostgreSQL has no single-file database, so its schema
fingerprint is a hash of a catalogue query rather than a file stat, and its presence forces
schema-qualified table identity, which this phase then applies to DuckDB and SQLite too.

**Tech Stack:** Python 3.11–3.13, `psycopg[binary]>=3.1,<4` (optional extra), sqlglot (`postgres`
dialect), pytest, PostgreSQL 16 via Docker locally and a GitHub Actions service container in CI.

## Global Constraints

Every task's requirements implicitly include this section.

- **Branch:** commit directly to `tuannm3812/main-refinement`. **No feature branches. Never
  `git commit --amend`.** Never `git add -A`; run `git status --short`, then stage explicit paths.
- **Run everything through `uv run`.** System `python3` is 3.9 and will fail.
- **`.devcontainer/devcontainer.json` has uncommitted edits by the owner.** Never stage, revert or
  touch that file.
- **Gates, all four, every task:** `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`. `text_to_sql_agent/` is `mypy --strict`.
- **`line-length = 100`.** Enforced by `ruff format`.
- **`requirements.txt` is generated** by
  `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`. Never edit it by hand.
  It must stay free of `psycopg` and `duckdb` — both are optional extras, and
  `tests/test_packaging.py` plus CI fail on drift.
- **Error codes are `SCREAMING_SNAKE_CASE` constants** (`docs/0_coding_standards.md` §3).
  PostgreSQL's abort code is `QUERY_ABORTED_AFTER_<n>_MS`, the same family member DuckDB uses.
- **The LLM never receives row data** beyond low-cardinality value hints
  (`docs/0_coding_standards.md` §4). A change to that needs a `docs/3_decisions.md` entry first.
- **No credential ever reaches a log, an error message, `evaluation/results/`, or the page.**
  A PostgreSQL DSN carries a password; `ui/uploads.py`'s `redact_dsn` is the existing masker.
- **The assembled SQLite system prompt stays byte-identical.** `tests/test_llm.py` pins its
  sha256 at `89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3`. If that test
  fails, fix your change — never the checksum.
- **Gold evaluation stays 12/12:** `uv run python scripts/evaluate_text_to_sql.py --mode gold`,
  then `git checkout evaluation/results/`.
- **Conventional Commits**, reasoning in the body. End every commit message with exactly:

  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```

**Owner decisions already made — do not relitigate:**

1. **PostgreSQL validates by default-deny**, like DuckDB: an `allowed_functions` allowlist plus
   the real-table/CTE rule, *on top of* the least-privilege role. Two independent defences.
2. **Local PostgreSQL tests read `AIPA_TEST_POSTGRES_DSN`** and skip with an explicit reason when
   it is unset. No `testcontainers` dependency. CI uses a service container and fails if any
   conformance test skipped.
3. **Schema-qualified table identity is in scope** (Task 6), for all three engines at once.

**Baseline at plan time:** commit `6c33993`, 447 tests passing, conformance 24 passed / 0 skipped,
mypy clean over 30 files, gold 12/12.

---

## File Structure

| Path | Responsibility |
|---|---|
| `text_to_sql_agent/engines/postgres.py` | **Create.** `PostgresEngine`: connection, read-only enforcement, execute, catalogue reads, allowlist. |
| `text_to_sql_agent/engines/__init__.py` | **Modify.** `open_engine` routes `postgresql://` and `postgres://`. |
| `text_to_sql_agent/engines/base.py` | **Modify.** Protocol gains `default_schema`; docstring for qualified `table_names()`. |
| `text_to_sql_agent/engines/duckdb.py` | **Modify (Task 6).** Stop filtering to `main`; emit qualified identity. |
| `text_to_sql_agent/engines/sqlite.py` | **Modify (Task 6).** `default_schema = "main"`; qualified `table_names()`. |
| `text_to_sql_agent/safety.py` | **Modify.** Accept a schema qualifier that names a real schema, not only `main`. |
| `text_to_sql_agent/types.py` | **Modify (Task 6).** `SchemaChunk` gains `schema_name` with a default. |
| `text_to_sql_agent/llm.py` | **Modify (Task 7).** Nothing dialect-specific — PostgreSQL's fragments live on its engine. |
| `ui/chat.py` | **Modify (Task 1).** Catch and redact pipeline exceptions. |
| `docker/postgres.yml` | **Create.** Compose file for a local PostgreSQL 16 plus the read-only role. |
| `docker/postgres-init.sql` | **Create.** Creates the least-privilege role the engine connects as. |
| `tests/conftest.py` | **Modify or create.** `postgres_dsn` fixture: env var or explicit skip. |
| `tests/test_engine_conformance.py` | **Modify.** Third fixture param. |
| `tests/test_engine_postgres.py` | **Create.** PostgreSQL-specific: privilege probes, allowlist, timeouts. |
| `tests/test_schema_identity.py` | **Create (Task 6).** Qualified identity across every engine. |
| `.github/workflows/tests.yml` | **Modify (Task 7).** Service container; DSN env var; no-skip guard extended. |

---

## Task 1: Test harness, and close the credential-leak path first

PostgreSQL driver errors commonly embed the DSN they failed to reach, and `ui/chat.py` currently
lets an exception from `ask_database_with_sql` escape to Streamlit, which renders the traceback
with no redaction (`docs/4_next_steps.md`, Phase 3b item 1). That path must be closed **before**
an engine whose errors carry passwords exists. No PostgreSQL code in this task.

**Files:**
- Create: `docker/postgres.yml`, `docker/postgres-init.sql`
- Create or modify: `tests/conftest.py`
- Modify: `ui/chat.py`
- Test: `tests/test_ui_chat.py` (create)

**Interfaces:**
- Produces: `postgres_dsn` pytest fixture returning a DSN `str`, skipping when
  `AIPA_TEST_POSTGRES_DSN` is unset or the server is unreachable. Every later task's PostgreSQL
  test consumes it.

- [ ] **Step 1: Write the failing UI test**

Create `tests/test_ui_chat.py`:

```python
"""The chat path must not let a driver exception reach the page unredacted."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import ui.chat as chat


def test_a_pipeline_exception_is_redacted_before_display() -> None:
    """A driver error echoing its DSN must not put a password on the page."""
    boom = RuntimeError("could not connect to postgresql://u:hunter2@db.example.com:5432/prod")
    with patch("ui.chat.backend.ask_database_with_sql", side_effect=boom):
        shown = chat.answer_question_text("list customers", db_path="postgresql://x", settings=None)

    assert "hunter2" not in shown
    assert "***" in shown
```

The exact seam depends on how `ui/chat.py` is shaped — read it first (the call site is around
line 120). If there is no function that returns displayable text, extract one: a small
`answer_question_text(...) -> str` (or a `_safe_ask` helper returning `tuple[str, QueryResult]`)
that wraps the `backend.ask_database_with_sql` call in `try/except Exception` and returns
`redact_dsn(f"{type(e).__name__}: {e}")`. Keep the extraction minimal and keep every existing
`st.*` call and session-state key unchanged — Streamlit persists widget state by key.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run pytest tests/test_ui_chat.py -v
```

Expected: FAIL — either `AttributeError` (no such function yet) or the password appearing in the
output.

- [ ] **Step 3: Implement the catch in `ui/chat.py`**

Wrap the `backend.ask_database_with_sql(...)` call. Import `redact_dsn` from `ui.uploads`, which
already masks `scheme://user:password@host` anywhere in a string. Render the redacted text through
the existing error surface (`ui/results.py`'s `describe_error` already redacts unrecognised text,
so routing through it is acceptable and preferable to a second code path).

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_ui_chat.py -v
uv run pytest
```

Expected: the new test passes; 448 total.

- [ ] **Step 5: Write the compose file and the read-only role**

`docker/postgres-init.sql` — runs on first container start:

```sql
-- The agent connects as this role. It can read every table in the sample
-- database and nothing else: no write, no DDL, and none of the file-reading
-- privileges (pg_read_server_files, pg_execute_server_program) that would let
-- pg_read_file() or COPY ... FROM PROGRAM reach the host filesystem.
CREATE ROLE aipa_ro LOGIN PASSWORD 'aipa_ro_pw';
GRANT CONNECT ON DATABASE aipa TO aipa_ro;
GRANT USAGE ON SCHEMA public TO aipa_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aipa_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aipa_ro;

CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE sales (
    sale_id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(customer_id)
);
INSERT INTO customers VALUES (1, 'Alice'), (2, 'Bob');
GRANT SELECT ON customers, sales TO aipa_ro;
```

`docker/postgres.yml`:

```yaml
# Local PostgreSQL for the engine tests. CI uses a service container instead;
# both create the same aipa_ro role via postgres-init.sql.
#
#   docker compose -f docker/postgres.yml up -d
#   export AIPA_TEST_POSTGRES_DSN=postgresql://aipa_ro:aipa_ro_pw@127.0.0.1:55432/aipa
#   uv run pytest
#
# Credentials here are local-only test values and are deliberately committed;
# nothing in this file is used by the app or the hosted demo.
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: aipa
      POSTGRES_PASSWORD: postgres
    ports:
      - "55432:5432"
    volumes:
      - ./postgres-init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d aipa"]
      interval: 2s
      timeout: 3s
      retries: 20
```

Port 55432 avoids colliding with a PostgreSQL the developer already runs on 5432.

- [ ] **Step 6: Write the `postgres_dsn` fixture**

In `tests/conftest.py` (create if absent):

```python
"""Shared fixtures.

PostgreSQL tests need a real server. They read `AIPA_TEST_POSTGRES_DSN` and skip
with an explicit reason when it is unset or unreachable, so a developer without
Docker can still run the suite. CI sets the variable and asserts nothing skipped,
so a silent skip cannot hide a broken engine.
"""

from __future__ import annotations

import os

import pytest

POSTGRES_DSN_ENV = "AIPA_TEST_POSTGRES_DSN"

_SKIP_REASON = (
    f"set {POSTGRES_DSN_ENV} to a reachable PostgreSQL DSN to run these "
    "(docker compose -f docker/postgres.yml up -d)"
)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """A DSN for a reachable PostgreSQL, or skip with an explicit reason."""
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(_SKIP_REASON)
    psycopg = pytest.importorskip("psycopg", reason="install the postgres extra")
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any connection failure means skip
        pytest.skip(f"{POSTGRES_DSN_ENV} is set but unreachable: {type(exc).__name__}")
    return dsn
```

- [ ] **Step 7: Prove the fixture skips and connects**

```bash
uv run pytest tests/test_ui_chat.py -q                      # unaffected
docker compose -f docker/postgres.yml up -d
AIPA_TEST_POSTGRES_DSN=postgresql://aipa_ro:aipa_ro_pw@127.0.0.1:55432/aipa \
  uv run python -c "import psycopg; print('server reachable')" || true
```

`psycopg` is not installed yet, so the second command may fail — that is expected here and Task 2
installs it. Record the actual output either way.

- [ ] **Step 8: Gates and commit**

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
git status --short
git add ui/chat.py tests/test_ui_chat.py tests/conftest.py docker/
git commit
```

Commit subject: `fix(ui): redact a pipeline exception before it reaches the page`.

---

## Task 2: `PostgresEngine` — connection, read-only proof, execute, abort code

**Files:**
- Create: `text_to_sql_agent/engines/postgres.py`
- Modify: `text_to_sql_agent/engines/__init__.py`, `pyproject.toml`, `requirements.txt` (regenerated)
- Create: `tests/test_engine_postgres.py`
- Modify: `tests/test_engine_conformance.py`

**Interfaces:**
- Consumes: `postgres_dsn` fixture (Task 1); the `Engine` protocol in `engines/base.py`.
- Produces: `PostgresEngine(dsn: str)` with `name = "postgres"`,
  `sqlglot_dialect = "postgres"`, `default_schema = "public"`, and every `Engine` method.
  `open_engine("postgresql://…")` and `open_engine("postgres://…")` both return it.

- [ ] **Step 1: Add the optional dependency**

In `pyproject.toml`:

```toml
[project.optional-dependencies]
duckdb = ["duckdb>=1.0,<2"]
postgres = ["psycopg[binary]>=3.1,<4"]
engines = ["duckdb>=1.0,<2", "psycopg[binary]>=3.1,<4"]
```

Then regenerate and confirm the driver stayed out:

```bash
uv sync --extra engines
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
grep -c psycopg requirements.txt    # expected: 0
grep -c duckdb requirements.txt     # expected: 0
uv run pytest tests/test_packaging.py -v
```

- [ ] **Step 2: Verify psycopg's real API before writing against it**

Do not trust this plan's API names. Run:

```bash
uv run python -c "
import psycopg
print(psycopg.__version__)
print(hasattr(psycopg.Connection, 'read_only'))
from psycopg import errors
print(errors.QueryCanceled, errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)
"
```

Expected: a 3.x version, `True`, and three exception classes. **If any of this differs, adapt and
say so in your report** — the mechanism matters, the spelling is yours to confirm.

- [ ] **Step 3: Write the conformance parametrisation first**

In `tests/test_engine_conformance.py`, extend the fixture params to
`["sqlite", "duckdb", "postgres"]` and add the branch. The suite deliberately contains no
per-engine assertions, so only the fixture changes:

```python
    if request.param == "postgres":
        dsn = os.environ.get("AIPA_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip(
                "set AIPA_TEST_POSTGRES_DSN to a reachable PostgreSQL DSN "
                "(docker compose -f docker/postgres.yml up -d)"
            )
        pytest.importorskip("psycopg", reason="install the postgres extra")
        return open_engine(dsn)
```

The compose file already creates `customers` and `sales` with the same shape the other two
branches build, which is what lets the shared assertions apply unchanged. The suite's `_add_table`
helper mutates the schema through a separate writable connection — for PostgreSQL that means
connecting as the `postgres` superuser, not `aipa_ro`; derive that DSN from the fixture's by
substituting user and password, and say in a comment why a second connection is needed.

- [ ] **Step 4: Run conformance and watch PostgreSQL fail**

```bash
docker compose -f docker/postgres.yml up -d
AIPA_TEST_POSTGRES_DSN=postgresql://aipa_ro:aipa_ro_pw@127.0.0.1:55432/aipa \
  uv run pytest -m conformance -v
```

Expected: 24 pass, 12 PostgreSQL cases fail — `open_engine` does not know the scheme yet
(`ValueError: unrecognised database scheme: 'postgresql'`). Record the output.

- [ ] **Step 5: Implement `PostgresEngine`**

Create `text_to_sql_agent/engines/postgres.py`. The read-only guarantee is **two mechanisms, both
mandatory** (`docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md` §4.2):

```python
"""PostgreSQL, proving read-only with a least-privilege role and a read-only transaction.

Neither alone is enough. The transaction flag is the per-statement guarantee and
survives a role that was granted too much; the role is what survives a driver or
pooler that resets session state between statements. `tests/test_engine_postgres.py`
proves each is load-bearing by removing it.
"""
```

`execute` must:

- open a connection with `connect_timeout`, set `conn.read_only = True` **before** the first
  statement, and run inside an explicit transaction;
- `SET LOCAL statement_timeout = <work_limit>` inside that transaction, so the budget cannot leak
  into another session;
- translate a cancelled statement into `QueryResult(..., error=f"QUERY_ABORTED_AFTER_{work_limit}_MS")`
  — the same family member DuckDB uses, per `docs/3_decisions.md` (2026-09-19);
- truncate at `max_rows` and set `error=f"RESULT_TRUNCATED_TO_{max_rows}_ROWS"`, matching the
  other engines;
- **raise** every other database error rather than returning it. `pipeline._repair_sql` depends on
  the exception to trigger repair; resource limits are typed codes, genuine SQL errors are
  exceptions. This mixed contract is deliberate — see `engines/duckdb.py`'s `execute` and copy its
  shape.

`check_reachable()` opens a connection and raises `EngineUnreachableError` on failure. **Its
message must never contain the DSN** — say `cannot connect to PostgreSQL` plus the exception
class name, never the connection string.

Leave `raw_schema`, `schema_chunks`, `schema_fingerprint` and `table_names` raising
`NotImplementedError` for now; Task 5 fills them. Conformance's read-only cases exercise
`execute` only.

- [ ] **Step 6: Route the scheme**

In `engines/__init__.py`, add both spellings before the final `raise ValueError`:

```python
    if scheme in {"postgresql", "postgres"}:
        try:
            from .postgres import PostgresEngine
        except ImportError as exc:
            raise EngineUnavailableError(
                "the 'postgres' extra is required for postgresql:// databases"
            ) from exc

        return PostgresEngine(dsn)
```

Note `dsn`, not `rest`: libpq needs the whole URL including its scheme, unlike the file-path
engines. Add a test in `tests/test_engines_resolution.py` pinning that both spellings resolve and
that the full DSN is preserved.

- [ ] **Step 7: Run conformance green**

```bash
AIPA_TEST_POSTGRES_DSN=postgresql://aipa_ro:aipa_ro_pw@127.0.0.1:55432/aipa \
  uv run pytest -m conformance -rs
```

Expected: 36 passed, 0 skipped. Without the env var: 24 passed, 12 skipped with the explicit
reason. Record both.

- [ ] **Step 8: Prove each read-only mechanism is load-bearing**

In `tests/test_engine_postgres.py`, and this is the point of the task:

```python
def test_the_read_only_transaction_is_load_bearing(postgres_dsn: str) -> None:
    """Removing the read-only transaction must let a write through.

    If this test passes with the flag removed, the flag is decoration and the
    role is carrying the whole guarantee - which is exactly the single point of
    failure Phase 3's design refused.
    """
```

Connect manually as `aipa_ro` **without** `read_only`, attempt an `INSERT`, and record what
happens. Because `aipa_ro` holds no `INSERT` grant you should see `InsufficientPrivilege` rather
than `ReadOnlySqlTransaction` — that proves the *role* is load-bearing. Then connect as the
superuser **with** `read_only = True`, attempt the same write, and expect
`ReadOnlySqlTransaction` — that proves the *transaction* is load-bearing. Two tests, one per
mechanism. Write both and report the actual exception classes.

- [ ] **Step 9: Gates and commit**

All four gates, plus conformance with and without the DSN. Commit subject:
`feat(engines): add PostgreSQL as the third engine`.

---

## Task 3: Probe the filesystem and privilege surface

DuckDB's `read_only=True` turned out not to stop filesystem access, and that was found only by
probing. PostgreSQL's analogues are `pg_read_file`, `pg_ls_dir`, `COPY … FROM PROGRAM`,
`lo_import`/`lo_export`, and the `dblink`/`postgres_fdw` extensions. Assume nothing; probe.

**Files:**
- Modify: `tests/test_engine_postgres.py`
- Modify: `text_to_sql_agent/engines/postgres.py` (only if a probe finds a hole)

- [ ] **Step 1: Probe, as `aipa_ro`, through `engine.execute` — bypassing `is_safe_query`**

```bash
AIPA_TEST_POSTGRES_DSN=... uv run python - <<'PY'
import os
from text_to_sql_agent.engines import open_engine

e = open_engine(os.environ["AIPA_TEST_POSTGRES_DSN"])
probes = [
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT * FROM pg_stat_file('/etc/passwd')",
    "SELECT current_setting('data_directory')",
    "SELECT * FROM pg_settings LIMIT 1",
    "SELECT usename, passwd FROM pg_shadow",
    "COPY (SELECT 1) TO PROGRAM 'touch /tmp/pwned'",
]
for sql in probes:
    try:
        print("ALLOWED", sql, "->", e.execute(sql, max_rows=3, work_limit=5000).rows)
    except Exception as exc:
        print("refused", sql, "->", type(exc).__name__)
PY
```

- [ ] **Step 2: Record every result in the report, verbatim**

Anything in the ALLOWED column is a finding. `current_setting` and `pg_settings` are readable by
any role and leak configuration, not files — note them; the allowlist in Task 4 is what closes
them. Anything that reads a *file* or runs a *program* as `aipa_ro` is a connection-layer hole and
must be fixed here, not in the validator, exactly as DuckDB's `enable_external_access=False` was.

- [ ] **Step 3: Pin the outcome with tests**

One test per probe asserting refusal, each with a docstring naming what it would mean if it
passed. These are the PostgreSQL equivalents of `tests/test_engine_duckdb.py`'s external-access
pins. Keep them in `tests/test_engine_postgres.py` — they have no SQLite or DuckDB equivalent, so
they do not belong in the shared conformance suite.

- [ ] **Step 4: Gates and commit**

Commit subject: `test(engines): pin PostgreSQL's file and program surface as refused`.

---

## Task 4: Default-deny validation for PostgreSQL

Owner's decision: the same stance DuckDB took after its blocklist leaked four times.

**Files:**
- Modify: `text_to_sql_agent/engines/postgres.py` (`allowed_functions`, internals lists)
- Modify: `text_to_sql_agent/safety.py` (only if the shared logic needs it)
- Modify: `tests/test_engine_postgres.py`

**Interfaces:**
- Consumes: `safety.is_safe_query(sql, *, engine)`, whose default-deny path runs when
  `engine.allowed_functions is not None`.
- Produces: `PostgresEngine.allowed_functions: frozenset[str]`.

- [ ] **Step 1: Enumerate PostgreSQL's real function surface**

```bash
AIPA_TEST_POSTGRES_DSN=... uv run python - <<'PY'
import os, psycopg
with psycopg.connect(os.environ["AIPA_TEST_POSTGRES_DSN"]) as c:
    rows = c.execute("""
        SELECT p.proname, n.nspname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname IN ('pg_catalog', 'public')
    """).fetchall()
print(len(rows), "functions")
PY
```

Expected: a few thousand. That count is the argument for default-deny — it is far too large to
blocklist, which is the same conclusion DuckDB reached at 945 names.

- [ ] **Step 2: Build the allowlist from what the agent actually needs**

Start from `DuckDBEngine.allowed_functions` (127 names, analytics-shaped: aggregates, string, date
and math functions, window functions). Keep the ones PostgreSQL spells the same, drop the
DuckDB-only ones, add PostgreSQL's own spellings. **Every name must be verified to exist** — run
each through `pg_proc` and report any that do not resolve.

- [ ] **Step 3: Round-trip the allowlist through sqlglot**

DuckDB needed `_FUNCTION_NAME_OVERRIDES` because sqlglot parses many calls into typed classes
whose `.sql_name()` is a cross-dialect token, not the engine's own name (`date_trunc` →
`TIMESTAMP_TRUNC`). PostgreSQL will hit the same problem. For every name in your allowlist:

```python
import sqlglot

parsed = sqlglot.parse_one(f"SELECT {name}(x) FROM t", read="postgres")
```

Resolve it the way `safety._resolve_function_name` does and assert the result is in the allowlist.
Any mismatch needs an override entry. Write this as a test, not a one-off script — it is the proof
that the allowlist means what a reviewer reads it to mean.

- [ ] **Step 4: Write the rejection and acceptance tests**

At minimum: `pg_read_file`, `current_setting`, `pg_sleep`, `dblink`, `query_to_xml` rejected;
a realistic analytics corpus accepted. Mirror `tests/test_engine_duckdb.py`'s corpus test — build
20+ queries a user would plausibly ask of `customers`/`sales` and assert every one passes
`is_safe_query`. A false rejection costs an unanswerable question; a false acceptance is a leak.

- [ ] **Step 5: Set the internals lists**

Per the design's §4.4 table: prefix `pg_`, name `information_schema`. Verify by probing —
`SELECT * FROM pg_catalog.pg_tables` and `SELECT * FROM information_schema.tables` must both be
rejected by `is_safe_query`, in a table-source position and as a schema qualifier.

- [ ] **Step 6: Gates and commit**

Commit subject: `feat(safety): default-deny function and table validation for PostgreSQL`.

---

## Task 5: PostgreSQL's schema layer

**Files:**
- Modify: `text_to_sql_agent/engines/postgres.py`
- Modify: `tests/test_engine_postgres.py`

**Interfaces:**
- Produces: `raw_schema() -> str`, `schema_chunks() -> list[SchemaChunk]`,
  `schema_fingerprint() -> tuple[object, ...]`, `table_names() -> frozenset[str]`.

- [ ] **Step 1: `raw_schema()` — synthesise DDL**

PostgreSQL has no `sqlite_master.sql` equivalent, so build `CREATE TABLE` text from
`information_schema.columns` plus `information_schema.table_constraints`. It feeds the LLM, so it
must read like DDL, not like a catalogue dump. Match the shape `SQLiteEngine.raw_schema()`
returns.

- [ ] **Step 2: `schema_chunks()` — columns, foreign keys, value hints**

Mirror `DuckDBEngine.schema_chunks()`. Two rules carried from the 2026-09-25 fix:

- Key columns and foreign keys by **(schema, table)**, never by bare table name. Keying by bare
  name is precisely the defect that crashed DuckDB on a duplicate name across schemas.
- Qualify the value-hint query's table reference, so it cannot resolve to a same-named table in
  another schema.

Value hints stay low-cardinality only — the standard's §4 row-data rule applies.

- [ ] **Step 3: `schema_fingerprint()` — a catalogue hash, not a stat**

Per the design's §4.8: a hash over table names, column names and types, and foreign keys, for the
connection's visible schemas. One catalogue query per cache check is the honest cost of a server
engine.

- [ ] **Step 4: Test that the fingerprint moves on DDL change and not otherwise**

Using the superuser connection: fingerprint, `CREATE TABLE`, fingerprint again — must differ.
Then `INSERT` a row and fingerprint again — must **not** differ, or every insert invalidates the
schema cache. Both assertions matter; the second is the one that catches a fingerprint built over
row counts.

- [ ] **Step 5: `table_names()`**

Reuse `schema.py`'s fingerprint-keyed chunk cache rather than issuing a fresh catalogue query, as
`base.py`'s docstring requires. Task 6 changes what this returns; implement the simple form now.

- [ ] **Step 6: Gates and commit**

Commit subject: `feat(engines): PostgreSQL schema extraction, chunks and fingerprint`.

---

## Task 6: Schema-qualified table identity, all three engines

The DuckDB `main`-only restriction (`docs/3_decisions.md`, 2026-09-25) was explicitly a
placeholder for this task. PostgreSQL makes it unavoidable: `public` is only a default, and real
databases spread tables across schemas.

**Files:**
- Modify: `text_to_sql_agent/types.py`, `engines/base.py`, `engines/sqlite.py`,
  `engines/duckdb.py`, `engines/postgres.py`, `safety.py`
- Create: `tests/test_schema_identity.py`

**Interfaces:**
- Produces: `SchemaChunk.schema_name: str` (defaulted, so `rag.py` is untouched);
  `Engine.default_schema: str`; `table_names()` returning **both** the bare name for
  default-schema tables and the `schema.table` form for every table.

- [ ] **Step 1: Write the cross-engine failing test**

`tests/test_schema_identity.py`, parametrised over every available engine, asserting that a table
in a non-default schema is advertised in `raw_schema()`, appears qualified in `table_names()`, and
that `is_safe_query("SELECT … FROM other.thing", engine=e)` is **True** while a nonexistent
`other.missing` is **False**. For SQLite, a second schema means `ATTACH DATABASE`; if that does
not fit the DSN model, state that in the report and cover SQLite with the default schema only
rather than inventing a shape the engine does not have.

- [ ] **Step 2: Run it and record the failures**

Expected: DuckDB fails because Task 5's predecessor filtered to `main`; PostgreSQL fails because
its catalogue reads are `public`-only.

- [ ] **Step 3: Add `schema_name` to `SchemaChunk` with a default**

Defaulted so nothing in `rag.py` — Phase 4's file, out of scope here — has to change. Add a
`qualified_name` property returning `schema.table` when the schema is not the engine's default,
and the bare name otherwise.

- [ ] **Step 4: Widen each engine's catalogue reads**

Remove the `main`-only filters from `engines/duckdb.py` (added 2026-09-25) and the `public`-only
assumption from `engines/postgres.py`. Every DDL string, chunk, value-hint query and foreign-key
reference now carries its schema. SQLite gains `default_schema = "main"` and keeps its behaviour.

- [ ] **Step 5: Teach `safety.py` the qualifier rule**

Today it accepts a schema qualifier only when it is literally `main`
(`text_to_sql_agent/safety.py`, around line 408). Replace with: a qualified reference is accepted
when its `schema.table` form is in `table_names()`. The internals check is unchanged and still
rejects `pg_catalog.*`, `information_schema.*` and `sqlite_*` regardless.

- [ ] **Step 6: Re-run the 2026-09-25 regression tests**

`tests/test_engine_duckdb.py`'s two multi-schema tests assert the *old* narrow behaviour — a table
outside `main` is never advertised. That contract is deliberately being replaced, so **update
them, do not delete them**: they become the tests that a duplicate name across schemas no longer
crashes and that both are now reachable by their qualified names. Preserving the crash
reproduction is the point.

- [ ] **Step 7: Full gates, plus the gold benchmark**

```bash
uv run pytest
AIPA_TEST_POSTGRES_DSN=... uv run pytest -m conformance -rs
uv run python scripts/evaluate_text_to_sql.py --mode gold && git checkout evaluation/results/
```

Gold must still be 12/12 — the demo databases are SQLite and single-schema, so a change here that
moves that figure means the qualifier rule broke the common path.

- [ ] **Step 8: Commit**

Commit subject: `feat(engines): carry schema-qualified table identity through every engine`.

---

## Task 7: Dialect prompt, end-to-end tests, and CI

**Files:**
- Modify: `text_to_sql_agent/engines/postgres.py` (prompt fragments)
- Modify: `tests/test_end_to_end_engines.py`, `tests/test_llm.py`
- Modify: `.github/workflows/tests.yml`

- [ ] **Step 1: Write PostgreSQL's three prompt fragments**

`prompt_dialect_section`, `prompt_dialect_name` (`"PostgreSQL"`) and `prompt_engine_rules_block`,
the same three the 2026-09-25 fix made engine-owned. The rules block must name PostgreSQL's own
internals (`pg_catalog`, `information_schema`), never `sqlite_master`. The dialect section should
mention what PostgreSQL actually wants: `EXTRACT`/`DATE_TRUNC`, `ILIKE` for case-insensitive
matching, and double-quoted identifiers.

- [ ] **Step 2: Extend the no-foreign-dialect test**

`tests/test_llm.py` already asserts, per engine, that no *other* engine's dialect name appears
outside its own section. Adding PostgreSQL to that parametrisation should be all that is needed —
confirm it fails first if you stub the fragments with SQLite's text, so you know the test covers
the new engine rather than passing vacuously.

- [ ] **Step 3: Extend the repair-dialect test**

`tests/test_pipeline.py` asserts the repair *user* prompt names the engine's dialect. Add
PostgreSQL.

- [ ] **Step 4: Extend the end-to-end matrix**

`tests/test_end_to_end_engines.py` currently parametrises `["sqlite", "duckdb"]` × two entry
points × RAG on/off. Add `postgres`, skipping without the DSN. That is 12 combinations.

- [ ] **Step 5: Add the CI service container**

In `.github/workflows/tests.yml`, `test` job:

```yaml
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_DB: aipa
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d aipa"
          --health-interval 2s --health-timeout 3s --health-retries 20
```

A service container cannot mount `docker/postgres-init.sql`, so add a step that applies it with
`psql` before the tests, and set `AIPA_TEST_POSTGRES_DSN` for the test steps. Keep the existing
"fail if any conformance test was skipped" guard — it now covers PostgreSQL too, which is what
stops a broken service container from going green.

- [ ] **Step 6: Push and confirm CI**

```bash
git push
gh run list --limit 1
gh run watch <id> --exit-status
```

All four jobs green. If `gh` is unavailable, say so rather than assuming.

- [ ] **Step 7: Commit**

Commit subject: `test(engines): end-to-end and CI coverage for PostgreSQL`.

---

## Task 8: Documentation and verification

**Files:**
- Modify: `docs/3_decisions.md`, `docs/0_coding_standards.md`, `docs/2_architecture.md`,
  `docs/4_next_steps.md`, `AGENTS.md`, `README.md`, `docs/6_agent_log.md`

- [ ] **Step 1: Record the decisions**

Dated entries in `docs/3_decisions.md`: PostgreSQL's two-mechanism read-only model; default-deny
for PostgreSQL and what its catalogue probe showed; the catalogue-hash fingerprint and its cost;
`AIPA_TEST_POSTGRES_DSN`-or-skip with CI asserting no skip; and schema-qualified identity,
superseding the 2026-09-25 `main`-only entry — **supersede, never edit** the older entry.

- [ ] **Step 2: Update the standards and architecture docs**

`docs/0_coding_standards.md` §3's engines convention gains PostgreSQL. `docs/2_architecture.md`
gains the third engine and a corrected test count. `AGENTS.md`'s "Current state" gains the date,
the new counts, and the `AIPA_TEST_POSTGRES_DSN` requirement for the full suite. `README.md` gains
a short "Running the PostgreSQL tests" note with the compose command.

- [ ] **Step 3: Move Phase 3b out of next-steps**

`docs/4_next_steps.md` leads with **Phase 4 — Real RAG**. Remove the items this phase closed
(the `ui/chat.py` gap, schema-qualified identity, the file-level fingerprint note where it no
longer applies) and keep the rest.

- [ ] **Step 4: Verify every figure**

```bash
uv run pytest 2>&1 | tail -1
AIPA_TEST_POSTGRES_DSN=... uv run pytest -m conformance -rs 2>&1 | tail -1
ls text_to_sql_agent/engines/*.py | wc -l
```

Any number in a doc must come from a command you ran in this task.

- [ ] **Step 5: Full gate**

```bash
uv sync --extra engines
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
AIPA_TEST_POSTGRES_DSN=... uv run pytest -m conformance -rs   # 36 passed, 0 skipped
uv run pytest -k "end_to_end" -v
uv run python -c "import app"
uv run python scripts/evaluate_text_to_sql.py --mode gold     # 12/12
git checkout evaluation/results/
```

- [ ] **Step 6: Write the agent log entry**

Append — never edit an existing entry. Include the Task 3 privilege probe output and the Task 4
catalogue count verbatim. **Do not write a claim you have not run a command to confirm.** Entries
in this log have already had to be corrected for exactly that.

- [ ] **Step 7: Push and confirm CI**

---

## Self-Review

**Spec coverage** against `docs/superpowers/specs/2026-09-14-phase-3-engine-abstraction-design.md`:
§4.2 PostgreSQL read-only → Task 2 Step 8. §4.3 conformance → Task 2 Step 3. §4.4 dialect-aware
safety → Task 4. §4.5 abort code → Task 2 Step 5. §4.6 optional extra → Task 2 Step 1. §4.7 UI
DSN → landed in Phase 3a; its credential-leak gap → Task 1. §4.8 reachability, schema dispatch and
fingerprint → Tasks 2 and 5. §4.9 dialect prompt → Task 7. §4.10 end-to-end → Task 7 Step 4. §6's
"PostgreSQL has analogues — `pg_read_file`, `COPY … FROM PROGRAM`, `lo_import`" → Task 3. §7
definition of done → Task 8 Step 5. Schema-qualified identity is **not** in that spec; it comes
from the 2026-09-25 decision and `docs/4_next_steps.md`, and is Task 6.

**Ordering.** Task 1 closes the credential-leak path before an engine whose errors carry passwords
exists. Conformance is parametrised (Task 2 Step 3) *before* the engine is written, so the suite
defines the target rather than describing what was built. The privilege probe (Task 3) precedes
the allowlist (Task 4), because what the connection layer already refuses changes what the
validator must carry. Schema identity (Task 6) lands after both engines can read their catalogues,
so the change has two real implementations to satisfy rather than one.

**Known risk this plan carries.** Task 6 changes a contract that Task 5 and the 2026-09-25 fix
both depend on. Its Step 6 says to *update* the multi-schema regression tests rather than delete
them, and Step 7 re-runs the gold benchmark, because the failure mode is silently losing the crash
reproduction while the suite still looks green.

**Deliberately not planned.** Migrating demo data to PostgreSQL; federated queries; connection
pooling; `rag.py`, which Phase 4 owns.
