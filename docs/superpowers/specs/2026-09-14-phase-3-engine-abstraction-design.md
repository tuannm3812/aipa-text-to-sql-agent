# Phase 3 — Engine Abstraction

**Date:** 2026-09-14
**Status:** Awaiting review
**Parent:** `2026-09-10-refactor-roadmap.md`
**Addresses:** Roadmap Phase 3
**Baseline standard:** `~/Documents/GitHub/coding-standards/coding_standards.md`

## 1. Purpose

Put a dialect/engine seam behind `schema.py`, `execution.py` and `safety.py`, port
SQLite onto it unchanged, and add **DuckDB and PostgreSQL** as second and third
implementations.

This must precede Phase 4: schema chunking is engine-specific, and rewriting RAG
first would mean rewriting it again once a second engine's schema shape arrives.

**The hard part is not the protocol — it is that the safety model does not
port.** SQLite's read-only guarantee is a connection URI flag, `PRAGMA
query_only`, and a 28-constant authorizer. Neither DuckDB nor PostgreSQL has
anything shaped like that. Per the owner's decision on 2026-09-14, **each engine
must prove read-only its own way**, against a shared conformance suite that every
engine has to pass.

## 2. Non-goals

- No RAG scoring changes. That is Phase 4.
- No agent loop. That is Phase 5.
- No UI redesign. The database picker gains engine options; nothing else moves.
- No migration of the demo databases. The three SQLite demos stay SQLite.
- No connection pooling, no async, no ORM.

## 3. Findings this phase acts on

All reproduced against the current tree on 2026-09-14; §9 has the commands.

### 3.1 How deep the SQLite coupling goes

| Module | SQLite references | What is coupled |
|---|---|---|
| `execution.py` | 37 (28 are authorizer constants) | connection URI, `PRAGMA query_only`, authorizer, VM-step progress handler |
| `schema.py` | 7 | `sqlite_master` queries, `PRAGMA table_info`, `PRAGMA foreign_key_list` |
| `safety.py` | 6 | `read="sqlite"` for sqlglot, the `sqlite_`/`pragma_`/`dbstat` internals rules |
| `ingestion.py`, `data_setup.py`, `llm.py` | 1 each | CSV ingest target, demo generation, the "SQLITE DIALECT" prompt section |

### 3.2 DuckDB: writes port, internals do not

Verified by probe:

- `duckdb.connect(path, read_only=True)` **natively refuses** `INSERT`, `UPDATE`,
  `DROP` and `CREATE`, each raising `InvalidInputException`. So the write half of
  the guarantee ports with no authorizer equivalent needed.
- **Internals are readable.** `SELECT * FROM duckdb_tables()` and
  `SELECT * FROM information_schema.tables` both succeed. So the internals
  blocklist is genuinely per-engine: SQLite's `sqlite_`/`pragma_`/`dbstat` names
  mean nothing here, and DuckDB needs its own (`duckdb_*` functions,
  `information_schema`, `pg_catalog`).
- `Connection.interrupt()` exists, so a work limit is implementable — though not
  via SQLite's progress-handler mechanism.

### 3.3 sqlglot covers all three dialects

`sqlglot.parse_one(sql, read=d)` works for `sqlite`, `duckdb` and `postgres`, so
`is_safe_query`'s parser is already dialect-parameterisable. Only the dialect
string and the internals names need to come from the engine.

### 3.4 The infrastructure for PostgreSQL tests exists

Docker is running on this machine, and CI runs on `ubuntu-latest`, which supports
GitHub Actions service containers. So PostgreSQL is testable both locally and in
CI without inventing a mocking layer.

### 3.5 The abort error code is SQLite-shaped

`QUERY_ABORTED_AFTER_<n>_VM_STEPS` names a SQLite implementation detail. A
timeout-based engine cannot produce it truthfully.

## 4. Design

### 4.1 The `Engine` protocol

A new `text_to_sql_agent/engines/` package. `base.py` defines:

```python
class Engine(Protocol):
    name: str  # "sqlite" | "duckdb" | "postgres"
    sqlglot_dialect: str  # passed to sqlglot's read=
    internal_prefixes: tuple[str, ...]
    internal_names: frozenset[str]

    def execute(self, sql: str, *, max_rows: int, work_limit: int) -> QueryResult: ...
    def schema_chunks(self) -> list[SchemaChunk]: ...
    def raw_schema(self) -> str: ...
```

Implementations: `engines/sqlite.py`, `engines/duckdb.py`, `engines/postgres.py`.
`engines/__init__.py` exposes `open_engine(dsn: str) -> Engine`, resolving by
scheme — a bare path or `sqlite://` to SQLite, `duckdb://` to DuckDB,
`postgresql://` to PostgreSQL.

`execute_query(db_path, sql, ...)` stays as a module-level function delegating to
`open_engine`, so `pipeline.py`, the notebook and the evaluation harness keep
working unchanged. Its signature does not change.

### 4.2 Read-only, proven per engine

| Engine | Mechanism |
|---|---|
| SQLite | `mode=ro` URI, `PRAGMA query_only`, the existing 28-constant authorizer. Unchanged. |
| DuckDB | `read_only=True` on connect, verified to refuse writes natively. |
| PostgreSQL | A dedicated read-only role **and** `BEGIN TRANSACTION READ ONLY` per statement. Both, not either: the transaction flag is the per-statement guarantee, the role is what survives a driver that resets state. |

### 4.3 The conformance suite is the real deliverable

`tests/test_engine_conformance.py` is parametrised over every available engine
and is what makes the abstraction meaningful rather than nominal. Every engine
must pass, with unavailable engines skipped explicitly and loudly, never silently.

It asserts, per engine:

1. A plain `SELECT` returns the expected rows and columns.
2. `INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE` are each refused **at the
   connection layer**, tested by calling `execute` directly and bypassing
   `is_safe_query` entirely — that is what proves defence in depth rather than a
   single gate.
3. Reading that engine's own internals is refused by `is_safe_query`.
4. The row cap returns `RESULT_TRUNCATED_TO_<n>_ROWS` with rows attached.
5. A runaway query returns a `QUERY_ABORTED_…` code rather than raising.
6. A genuine SQL error — a missing table — still **raises**, because
   `pipeline.py` depends on the exception to trigger repair. This is the mixed
   contract `docs/4_next_steps.md` records, and getting it wrong in a new engine
   either disables repair or resurrects the wasted LLM call on an aborted query.
7. `schema_chunks()` returns table names, columns and foreign-key neighbours for
   a fixture database with a known shape.

### 4.4 Safety becomes dialect-aware

`is_safe_query(sql, *, engine)` takes the engine and uses its
`sqlglot_dialect`, `internal_prefixes` and `internal_names`. The structural rules
— single statement, `SELECT`/`WITH` prefix, no data-modifying node, table-source
position for the internals check — are engine-independent and stay shared.

Per-engine internals lists, from §3.2 and the PostgreSQL catalogue:

| Engine | Prefixes | Names |
|---|---|---|
| SQLite | `sqlite_`, `pragma_` | `dbstat` |
| DuckDB | `duckdb_` | `information_schema`, `pg_catalog` |
| PostgreSQL | `pg_` | `information_schema` |

The DuckDB and PostgreSQL lists are **starting points, not verified complete**.
Each must be probed the way SQLite's was — by enumerating the engine's own
catalogue and trying to reach it — before the phase closes. Phase 2 shipped an
internals rule that looked complete and had a `dbstat('main')` bypass in it.

**`is_safe_query`'s signature changes.** It is exported in `__all__` and called
from `pipeline.py`, `ui/evaluation.py` and the evaluation module. The engine
parameter gets a default of the SQLite engine so existing callers keep working,
and every call site is updated to pass its engine explicitly in the same commit.

### 4.5 The abort code generalises

`QUERY_ABORTED_AFTER_<n>_VM_STEPS` becomes one instance of a family sharing the
`QUERY_ABORTED_AFTER_` prefix, with the suffix naming the engine's own unit:

- SQLite → `QUERY_ABORTED_AFTER_100000_VM_STEPS` (unchanged)
- DuckDB → `QUERY_ABORTED_AFTER_5000_MS`
- PostgreSQL → `QUERY_ABORTED_AFTER_5000_MS` (`statement_timeout`)

`ui/results.py`'s `describe_error` already prefix-matches `QUERY_ABORTED_AFTER_`
and strips `_VM_STEPS`; it must handle both suffixes and render a message that
reads correctly for each. SQLite's existing code and its tests are unchanged, so
no reported figure moves.

### 4.6 Optional dependencies

`pyproject.toml` gains:

```toml
[project.optional-dependencies]
duckdb = ["duckdb>=1.0,<2"]
postgres = ["psycopg[binary]>=3.1,<4"]
engines = ["duckdb>=1.0,<2", "psycopg[binary]>=3.1,<4"]
```

Neither is a runtime dependency. The hosted Streamlit demo installs from the
generated `requirements.txt`, which stays SQLite-only, so the demo does not grow
a database driver it never uses. CI installs `engines` so conformance runs.

Engine modules import their driver lazily inside `open_engine`, and a missing
driver raises a clear `EngineUnavailableError` naming the extra to install —
never a bare `ImportError`.

### 4.7 The UI gains engines without changing shape

`ui/constants.py`'s `DEMO_DATABASES` stays SQLite. The Database radio gains a
"Connection string" option accepting a DSN. The chat and evaluation paths take a
DSN where they currently take a path. No layout changes.

**PostgreSQL credentials are never persisted.** The DSN lives in
`st.session_state` for the session only, is never written to disk, and is
redacted in any error surfaced to the page — a connection failure must not print
a password back to the user.

## 5. Sub-phases

The owner chose to deliver both engines in one phase. To keep each half
independently reviewable and to stop PostgreSQL's infrastructure risk blocking
DuckDB, this spec produces **two implementation plans**:

- **3a — protocol, SQLite port, DuckDB.** No new infrastructure; `duckdb` is one
  pip install and runs in-process. Ends with the conformance suite passing for
  two engines.
- **3b — PostgreSQL.** Adds the CI service container, the testcontainer-or-skip
  local strategy, the read-only role, and DSN handling in the UI.

3a must land and be green before 3b starts. If 3b proves larger than expected,
3a is a coherent, shippable result on its own.

## 6. Risks

**The abstraction encodes SQLite's assumptions.** An interface extracted from one
implementation usually fits only that implementation. Mitigation: DuckDB lands in
the same sub-phase as the protocol, so the second implementation is written
against the interface immediately rather than after it has set. If the protocol
needs changing to fit DuckDB, that is the protocol working, not a failure.

**A new engine weakens the safety guarantee without anyone noticing.** This is
the serious one. Mitigation: conformance test 2 bypasses `is_safe_query` and
attacks the connection directly, so an engine whose read-only enforcement is
nominal fails. No engine may be added without passing the suite.

**The internals blocklists are incomplete.** Assume they are, because SQLite's
was. Each engine's list must be probed against its own catalogue before the phase
closes, and the probe recorded.

**PostgreSQL tests are flaky or unavailable.** Docker exists locally and CI
supports service containers, but a developer without Docker must still be able to
run the suite. Mitigation: PostgreSQL tests skip with an explicit reason when no
server is reachable, and CI asserts they were **not** skipped, so a silent skip
cannot hide a broken engine.

**Credential leakage.** A DSN carries a password. It must never reach a log, an
error message, `evaluation/results/`, or the page.

## 7. Definition of done

```
uv sync --extra engines
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                      # conformance green for sqlite, duckdb, postgres
uv run pytest -m conformance -v    # every engine listed, none silently skipped
uv run python -c "import app"
uv run streamlit run app.py        # SQLite demos unchanged; a DuckDB file queryable
uv run python scripts/evaluate_text_to_sql.py --mode gold   # still 12/12
```

CI green across 3.11-3.13 with the PostgreSQL service container, and the
conformance job asserting no engine was skipped. `docs/3_decisions.md` records
the per-engine read-only decision and the abort-code generalisation.
`docs/6_agent_log.md` gains an entry stating what was verified, including each
engine's internals probe.

## 8. Out of scope, deliberately

- Migrating demo data to DuckDB or PostgreSQL.
- Cross-engine federated queries.
- Connection pooling and async execution.
- `rag.py`, which Phase 4 owns — though this phase must leave `schema_chunks()`
  engine-agnostic enough that Phase 4 does not have to unpick it again.

## 9. Reproducing the findings

```bash
# 3.1 coupling depth
grep -rc "sqlite3\.\|sqlite_" --include="*.py" text_to_sql_agent/

# 3.2 DuckDB: writes refused, internals readable
uv run --with duckdb python -c "
import duckdb, tempfile, os
p=os.path.join(tempfile.mkdtemp(),'t.duckdb')
c=duckdb.connect(p); c.execute('CREATE TABLE t (a INTEGER)'); c.close()
ro=duckdb.connect(p, read_only=True)
try: ro.execute('INSERT INTO t VALUES (1)')
except Exception as e: print('write refused:', type(e).__name__)
print('internals rows:', len(ro.execute('SELECT * FROM duckdb_tables()').fetchall()))"

# 3.3 sqlglot dialects
uv run python -c "
import sqlglot
[sqlglot.parse_one('SELECT 1', read=d) for d in ('sqlite','duckdb','postgres')]
print('all three dialects parse')"

# 3.4 infrastructure
docker info >/dev/null 2>&1 && echo docker-ok
```
