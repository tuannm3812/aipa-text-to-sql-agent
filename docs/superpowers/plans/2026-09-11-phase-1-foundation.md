# Phase 1 — Foundation and Standards Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring `aipa-text-to-sql-agent` onto the master coding standard with `uv`-managed packaging, ruff linting, agent instructions, Shape B docs, and a per-module test suite — changing no runtime behaviour.

**Architecture:** Nine tasks, each ending in a green test run and one Conventional Commit. Tooling lands first (Task 1) so every later task is linted and tested by the same gate. The riskiest change — deleting the 170-line duplicate shim — is Task 4, after the test suite has been split so failures point at one module. Docs and CI land last, once the paths they reference are final.

**Tech Stack:** Python 3.11-3.13, `uv` 0.11+, hatchling, ruff 0.16.4, mypy, pytest 8, Streamlit, SQLite.

## Global Constraints

- **No runtime behaviour changes.** If a ruff or mypy finding requires changing what the code *does*, suppress it with `# noqa: <rule>  # Phase 2` and move on. Fixing it here is out of scope.
- **Work commits directly to `tuannm3812/main-refinement`.** No feature branches — single-owner project. Never force-push.
- **Conventional Commits, scoped and imperative** (master §9): `<type>(<scope>): <imperative summary>`. Put material detail in the body: what changed, what was verified, what was not.
- **Before every commit** run `git status --short` and review every path (master §10). Never `git add -A`.
- **Never claim a check passed without running it and reading the output** (master §10).
- `line-length = 100`, `target-version = "py311"`, `requires-python = ">=3.11,<3.14"`.
- `ruff` is pinned to exactly `0.16.4`. Everything else uses bounded ranges, never exact pins.
- Python on this machine is **3.9.6**, which cannot run this project. Every command below therefore goes through `uv run`, which provisions 3.11. A bare `python3`/`pytest` invocation will fail with syntax errors on `X | Y` annotations — that is the wrong interpreter, not a real failure.
- The master standard is at `~/Documents/GitHub/coding-standards/coding_standards.md`. Never copy it into this repo; reference it (master §13).

---

## File Structure

**Created:**

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Single source of truth for dependencies and all tool config |
| `uv.lock` | Resolved dependency graph, committed |
| `AGENTS.md` | Master §13 layer 2 — repo identity, deltas, evidence, risks |
| `CLAUDE.md` | One line: `@AGENTS.md` |
| `tests/conftest.py` | Shared pytest fixtures (temp SQLite builders) |
| `tests/test_gemini_manager.py` | Key-failover behaviour |
| `tests/test_safety.py` | `is_safe_query` allow/block |
| `tests/test_ingestion.py` | CSV ingestion and table-name sanitising |
| `tests/test_schema.py` | Schema extraction |
| `tests/test_rag.py` | Schema retrieval and synonym expansion |
| `tests/test_execution.py` | Row caps and read-only enforcement |
| `tests/test_pipeline.py` | End-to-end `ask_database` |
| `tests/test_packaging.py` | `pyproject.toml` vs `requirements.txt` drift guard |
| `docs/0_coding_standards.md` | Project deltas only — never restates the master |
| `docs/1_brief.md` | What is built, for whom, what done looks like |
| `docs/3_decisions.md` | Dated decision log |
| `docs/4_next_steps.md` | Phases 2-5 |
| `docs/6_agent_log.md` | Append-only work record |

**Modified:** `app.py` (import line only), `scripts/evaluate_text_to_sql.py` (import line only), `text_to_sql_agent_mvp.ipynb` (import cell + a dated note), `requirements.txt` (regenerated, header added), `.gitignore` (§8 negation block, `.DS_Store`), `.github/workflows/tests.yml` (rewritten), `README.md` (paths, tree, Python version), `.devcontainer/devcontainer.json` (uv), `text_to_sql_agent/config.py` (`# fmt:` guards), `text_to_sql_agent/*.py` (docstrings, annotations).

**Deleted:** `text_to_sql_agent_mvp.py`, `tests/test_text_to_sql_agent.py`, `docs/.DS_Store`, `docs/supporting/` (contents moved).

---

### Task 1: Packaging and lint tooling

Establishes the gate every later task runs against. No source files change yet.

**Files:**
- Create: `pyproject.toml`, `uv.lock`
- Modify: `requirements.txt`, `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `uv run <cmd>` works; `uv run ruff check .`, `uv run mypy text_to_sql_agent`, `uv run pytest` are the three gate commands every later task uses.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "aipa-text-to-sql-agent"
version = "0.1.0"
description = "Enterprise Text-to-SQL agent: natural language to safe, read-only SQLite queries."
readme = "README.md"
requires-python = ">=3.11,<3.14"
dependencies = [
  "google-genai>=1.0,<2",
  "langchain-core>=0.3,<0.4",
  "langchain-ollama>=0.2,<0.3",
  "pandas>=2.1,<3",
  "python-dotenv>=1.0,<2",
  "sqlglot>=25,<28",
  "streamlit>=1.28,<2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["text_to_sql_agent"]

[dependency-groups]
dev = [
  "mypy>=1.11",
  "pytest>=8",
  "ruff==0.16.4",
]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "SIM", "D"]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"*.ipynb" = ["E501", "D"]
"tests/*" = ["D"]
"app.py" = ["D"]
"scripts/*" = ["D"]
# Long lines in llm.py are inside SQL_TRANSLATION_SYSTEM_PROMPT. A line break
# there is content the model reads, so rewrapping would change behaviour.
"text_to_sql_agent/llm.py" = ["E501"]

[tool.mypy]
python_version = "3.11"
files = ["text_to_sql_agent"]
strict = true

[[tool.mypy.overrides]]
module = ["google.genai.*", "langchain_ollama.*", "sqlglot.*"]
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = "-q"
```

- [ ] **Step 2: Resolve and lock**

Run: `uv sync`
Expected: `uv` downloads CPython 3.11+ if absent, writes `uv.lock`, and prints an
`Installed N packages` line. If it fails to resolve `sqlglot<28`, widen only that
bound and note it in the commit body — do not remove bounds wholesale.

- [ ] **Step 3: Verify the interpreter is what the plan assumes**

Run: `uv run python -c "import sys; print(sys.version)"`
Expected: `3.11.x`, `3.12.x`, or `3.13.x` — **not** 3.9.6.

- [ ] **Step 4: Regenerate `requirements.txt` with a provenance header**

```bash
uv export --no-hashes --no-dev --no-emit-project -o requirements.txt
```

Then prepend the header, so the next reader does not hand-edit it:

```bash
printf '%s\n%s\n%s\n\n' \
  '# GENERATED FILE - do not edit by hand.' \
  '# Source of truth: pyproject.toml. Regenerate with:' \
  '#   uv export --no-hashes --no-dev --no-emit-project -o requirements.txt' \
  | cat - requirements.txt > /tmp/req.new && mv /tmp/req.new requirements.txt
```

- [ ] **Step 5: Verify the generated manifest still installs standalone**

Streamlit Cloud reads this file, so it must work without `uv`:

```bash
python3 -m venv /tmp/reqcheck && /tmp/reqcheck/bin/pip install -q -r requirements.txt && echo "REQUIREMENTS OK"
```

Expected: `REQUIREMENTS OK`. If it fails on the local 3.9.6 venv because a pin
needs 3.11+, rerun with `uv venv --python 3.11 /tmp/reqcheck` instead. Record
which one you used in the commit body.

- [ ] **Step 6: Add the master §8 gitignore block**

Append to `.gitignore`:

```gitignore
# macOS
.DS_Store

# Demo fixtures (master coding standard §8: ignore the class, negate the
# exceptions). These six files are tracked deliberately - app.py offers them as
# the built-in demo databases and evaluation/cases.json runs against them, so
# the hosted demo is non-functional without them.
data/**/*.db
data/**/*.csv
!data/customers.csv
!data/sales.csv
!data/dynamic_agent.db
!data/healthcare_analytics.db
!data/retail_analytics.db
!data/university_agent.db
```

- [ ] **Step 7: Verify the negation block changed nothing tracked**

Run: `git status --short && git ls-files data/ | wc -l`
Expected: `6` tracked files under `data/`, and **no** deletions in `git status`.
If any `data/` file shows as deleted, a negation line is wrong — fix it before
committing.

- [ ] **Step 8: Remove the stray macOS file**

```bash
git rm --cached docs/.DS_Store && rm -f docs/.DS_Store
```

- [ ] **Step 9: Commit**

```bash
git status --short
git add pyproject.toml uv.lock requirements.txt .gitignore
git rm --cached docs/.DS_Store 2>/dev/null || true
git commit -m "chore(tooling): add pyproject, uv lockfile, and ruff/mypy config

Makes pyproject.toml the source of truth for dependencies and tool config,
per the master coding standard. requirements.txt stays at the root because
Streamlit Cloud and the devcontainer read it, but is now generated by
uv export and carries a header saying so.

Runtime deps gain upper bounds so a major release of a transitive
dependency cannot silently break the hosted demo. ruff is pinned exactly
so an upstream release cannot turn an unrelated commit red.

Adds the master §8 ignore-then-negate block for the six demo fixtures
under data/, which .gitignore previously said nothing about. Verified no
tracked file changed state.

Verified: uv sync resolves; uv run python reports 3.11+; requirements.txt
installs into a clean venv."
```

---

### Task 2: Split the test suite by module

Done before any source change, so later failures point at one module.

**Files:**
- Create: `tests/conftest.py`, `tests/test_gemini_manager.py`, `tests/test_safety.py`, `tests/test_ingestion.py`, `tests/test_schema.py`, `tests/test_rag.py`, `tests/test_execution.py`, `tests/test_pipeline.py`
- Delete: `tests/test_text_to_sql_agent.py`

**Interfaces:**
- Consumes: `uv run pytest` from Task 1.
- Produces: three fixtures, each returning a filesystem path string to a temp SQLite database: `customers_db` (one table, one row), `customers_courses_db` (adds an unrelated `courses` table), and `customers_sales_courses_db` (adds `sales` with a foreign key to `customers`). Task 4 edits `tests/test_pipeline.py` and `tests/test_rag.py`.

Classes are renamed to `Test<Module>` because `python_classes = ["Test*"]` from
Task 1 will not collect `TextToSqlAgentTests`. Bodies are otherwise moved
verbatim — this task changes no assertions.

- [ ] **Step 1: Record the baseline before touching anything**

Run: `uv run pytest`
Expected: `19 passed`. Write the number down; every later step compares to it.

- [ ] **Step 2: Create `tests/conftest.py`**

Two fixtures replace the `tempfile.TemporaryDirectory()` blocks repeated across
nine tests. `tmp_path` is a pytest builtin giving a per-test temp directory.

```python
"""Shared fixtures for the text-to-sql agent test suite."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


@pytest.fixture
def customers_db(tmp_path: Path) -> str:
    """A database with one `customers` table holding a single row."""
    db_path = tmp_path / "customers.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_courses_db(tmp_path: Path) -> str:
    """`customers` (one row) plus an unrelated `courses` table.

    Used where a test needs one clearly relevant table and one clearly
    irrelevant one, to assert that retrieval excludes the latter.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.execute("INSERT INTO customers VALUES (1, 'Alice')")
        conn.commit()
    return str(db_path)


@pytest.fixture
def customers_sales_courses_db(tmp_path: Path) -> str:
    """Three tables where `sales` has a foreign key to `customers`.

    `courses` is deliberately unrelated, so retrieval tests can assert it is
    *not* selected.
    """
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            "CREATE TABLE sales (sale_id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL, "
            "FOREIGN KEY(customer_id) REFERENCES customers(customer_id))"
        )
        conn.execute("CREATE TABLE courses (course_id INTEGER PRIMARY KEY, course_name TEXT)")
        conn.commit()
    return str(db_path)
```

- [ ] **Step 3: Create `tests/test_gemini_manager.py`**

Moves lines 16-84 of the old file. This is the only new file that does not
import the agent package.

```python
from __future__ import annotations

import unittest
from unittest.mock import patch

from text_to_sql_agent.gemini_manager import GeminiManager, load_google_api_keys


class FakeGoogleError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class TestGeminiManager(unittest.TestCase):
    def test_gemini_manager_loads_multiple_key_env_vars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_API_KEYS": "key-a, key-b",
                "GEMINI_API_KEY": "key-c",
            },
            clear=True,
        ):
            self.assertEqual(load_google_api_keys(), ["key-a", "key-b", "key-c"])

    def test_gemini_manager_loads_indexed_key_env_vars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_API_KEY": "key-a",
                "GOOGLE_API_KEY_2": "key-c",
                "GOOGLE_API_KEY_1": "key-b",
                "GEMINI_API_KEY_3": "key-d",
            },
            clear=True,
        ):
            self.assertEqual(load_google_api_keys(), ["key-a", "key-b", "key-c", "key-d"])

    def test_gemini_manager_moves_to_next_key_on_492(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if api_key == "key-a":
                raise FakeGoogleError(492)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-b"])

    def test_gemini_manager_resets_key_once_on_503(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if len(calls) == 1:
                raise FakeGoogleError(503)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-a"])

    def test_gemini_manager_advances_after_repeated_503_for_same_key(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if api_key == "key-a":
                raise FakeGoogleError(503)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-a", "key-b"])
```

- [ ] **Step 4: Create `tests/test_safety.py`**

Still imports the shim (`text_to_sql_agent_mvp`); Task 4 repoints it.

```python
from __future__ import annotations

import unittest

import text_to_sql_agent_mvp as agent


class TestSafety(unittest.TestCase):
    def test_is_safe_query_allows_read_only_queries(self) -> None:
        self.assertTrue(agent.is_safe_query("SELECT name FROM customers;"))
        self.assertTrue(
            agent.is_safe_query(
                "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM sales "
                "GROUP BY customer_id) SELECT * FROM totals"
            )
        )

    def test_is_safe_query_blocks_unsafe_sql(self) -> None:
        for sql in [
            "DELETE FROM customers",
            "DROP TABLE customers",
            "UPDATE customers SET name = 'x'",
            "PRAGMA table_info(customers)",
            "SELECT * FROM sqlite_master",
        ]:
            with self.subTest(sql=sql):
                self.assertFalse(agent.is_safe_query(sql))
```

Note the first test's long SQL string is split across two lines to fit the
100-character limit. This is a string literal passed to a parser, so the
concatenation is byte-equivalent apart from the single space, which SQL treats
as insignificant whitespace — unlike the `llm.py` prompt, where breaks matter.

- [ ] **Step 5: Create `tests/test_ingestion.py`**

```python
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

import pandas as pd

import text_to_sql_agent_mvp as agent


class TestIngestion(unittest.TestCase):
    def test_normalize_table_name(self) -> None:
        self.assertEqual(
            agent.normalize_table_name("Sales Report", {"sales_report"}), "sales_report_2"
        )
        self.assertEqual(agent.normalize_table_name("2024 Sales!", set()), "table_2024_sales")
        self.assertEqual(agent.normalize_table_name("!!!", set()), "table")


def test_ingest_csvs_to_db_sanitizes_table_names(tmp_path: Path) -> None:
    csv_path = tmp_path / "2024 Sales Report.csv"
    pd.DataFrame({"amount": [10, 20]}).to_csv(csv_path, index=False)
    db_path = tmp_path / "out.db"

    agent.ingest_csvs_to_db([str(csv_path)], str(db_path))

    with closing(sqlite3.connect(db_path)) as conn:
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        rows = conn.execute("SELECT amount FROM table_2024_sales_report").fetchall()

    assert tables == [("table_2024_sales_report",)]
    assert rows == [(10,), (20,)]
```

The second test becomes a plain function so it can take `tmp_path`. A
`unittest.TestCase` method cannot receive pytest fixtures — this is why the split
mixes both styles rather than keeping one class per file.

- [ ] **Step 6: Create `tests/test_schema.py`**

```python
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import text_to_sql_agent_mvp as agent


def test_get_schema_excludes_internal_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()

    schema = agent.get_schema(str(db_path))

    assert "CREATE TABLE customers" in schema
    assert "sqlite_" not in schema
```

- [ ] **Step 7: Create `tests/test_rag.py`**

```python
from __future__ import annotations

import text_to_sql_agent_mvp as agent


def test_retrieve_schema_chunks_selects_relevant_tables(
    customers_sales_courses_db: str,
) -> None:
    chunks = agent.retrieve_schema_chunks(
        customers_sales_courses_db,
        "total sales amount by customer",
        top_k=1,
    )
    names = {chunk.table_name for chunk in chunks}

    assert "sales" in names
    assert "customers" in names
    assert "courses" not in names


def test_schema_rag_expands_business_synonyms(customers_sales_courses_db: str) -> None:
    context = agent.retrieve_schema_context(
        customers_sales_courses_db,
        "revenue by client",
        top_k=1,
    )
    names = {chunk.table_name for chunk in context.chunks}

    assert "sales" in names
    assert "customers" in names
    assert "revenue" in context.expanded_tokens
    assert "customer" in context.expanded_tokens
    assert "Schema RAG strategy" in context.report


def test_retrieve_relevant_schema_returns_only_selected_ddl(customers_courses_db: str) -> None:
    schema = agent.retrieve_relevant_schema(customers_courses_db, "customer names", top_k=1)

    assert "CREATE TABLE customers" in schema
    assert "CREATE TABLE courses" not in schema
```

- [ ] **Step 8: Create `tests/test_execution.py`**

```python
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import text_to_sql_agent_mvp as agent


def test_execute_query_caps_rows_and_reports_truncation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE numbers (n INTEGER)")
        conn.executemany("INSERT INTO numbers VALUES (?)", [(1,), (2,), (3,)])
        conn.commit()

    result = agent.execute_query(str(db_path), "SELECT n FROM numbers ORDER BY n", max_rows=2)

    assert result.rows == [(1,), (2,)]
    assert result.error == "RESULT_TRUNCATED_TO_2_ROWS"


def test_execute_query_opens_database_read_only(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE numbers (n INTEGER)")
        conn.commit()

    with pytest.raises(sqlite3.DatabaseError):
        agent.execute_query(str(db_path), "DELETE FROM numbers")
```

- [ ] **Step 9: Create `tests/test_pipeline.py`**

Patch targets still name the shim here. Task 4 changes them — that is the whole
point of doing this split first.

```python
from __future__ import annotations

from unittest.mock import patch

import text_to_sql_agent_mvp as agent


def test_ask_database_uses_retrieved_schema_by_default(customers_courses_db: str) -> None:
    captured_schema: dict[str, str] = {}

    def fake_generate_sql(_question: str, schema_text: str, **_kwargs: object) -> str:
        captured_schema["text"] = schema_text
        return "SELECT name FROM customers"

    with patch.object(agent, "generate_sql", side_effect=fake_generate_sql):
        result = agent.ask_database(
            "list customer names", db_path=customers_courses_db, rag_top_k=1
        )

    assert result.ok
    assert "CREATE TABLE customers" in captured_schema["text"]
    assert "CREATE TABLE courses" not in captured_schema["text"]


def test_ask_database_blocks_unsafe_generated_sql(customers_db: str) -> None:
    with patch.object(agent, "generate_sql", return_value="DROP TABLE customers"):
        result = agent.ask_database("remove customers", db_path=customers_db)

    assert not result.ok
    assert result.error == "BLOCKED_UNSAFE_SQL"
    assert result.sql == "DROP TABLE customers"


def test_ask_database_executes_safe_generated_sql(customers_db: str) -> None:
    with patch.object(agent, "generate_sql", return_value="SELECT name FROM customers"):
        result = agent.ask_database("list customers", db_path=customers_db)

    assert result.ok
    assert result.columns == ["name"]
    assert result.rows == [("Alice",)]
```

- [ ] **Step 10: Delete the old file and run the suite**

```bash
git rm tests/test_text_to_sql_agent.py
uv run pytest
```

Expected: `19 passed`, matching the Step 1 baseline exactly. A count of 18 means
a test was dropped in the move; a count of 14 means `python_classes` did not
collect a renamed class. Do not proceed until it reads 19.

- [ ] **Step 11: Commit**

```bash
git status --short
git add tests/
git commit -m "test: split the suite into one file per module

Breaks the single 284-line test_text_to_sql_agent.py into seven files
matching the package's modules, and moves the repeated temp-database
setup into conftest.py fixtures.

Test bodies are moved verbatim; the only edits are class renames to
Test<Module> (pytest's python_classes setting will not collect the old
TextToSqlAgentTests name) and converting tests that need a temp database
from TestCase methods to plain functions, since a TestCase method cannot
receive a pytest fixture.

Patch targets still name the compatibility shim. Repointing them is the
next commit, deliberately kept separate so a failure there is
unambiguous.

Verified: uv run pytest reports 19 passed, matching the pre-split
baseline."
```

---

### Task 3: Docstrings, formatting, and the first clean lint

**Files:**
- Modify: `text_to_sql_agent/config.py`, and any file ruff flags

**Interfaces:**
- Consumes: the ruff config from Task 1.
- Produces: `uv run ruff check .` and `uv run ruff format --check .` both exit 0. Every later task must keep them at 0.

- [ ] **Step 1: See the full scale of the problem before changing anything**

Run: `uv run ruff check . --statistics`
Expected: a table of rule codes and counts. Read it. If `D` alone exceeds ~80
findings, apply the spec's documented fallback — narrow `select` from `"D"` to
`"D1"` in `pyproject.toml`, note it in the commit body, and move the prose pass
to Phase 2.

- [ ] **Step 2: Protect the hand-aligned STOPWORDS block**

`ruff format` would reflow this into one token per line. The current column
alignment is deliberate. In `text_to_sql_agent/config.py`, wrap the trailing
`STOPWORDS = {...}` assignment:

```python
# fmt: off
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "how", "in",
    "is", "it", "me", "of", "on", "or", "per", "show", "the", "to", "total",
    "what", "which", "with",
}
# fmt: on
```

- [ ] **Step 3: Apply mechanical fixes and formatting**

```bash
uv run ruff check . --fix
uv run ruff format .
```

- [ ] **Step 4: Confirm the formatter left the prompt untouched**

The prompt string is behaviour. Verify it did not move:

```bash
git diff --stat text_to_sql_agent/llm.py
```

Expected: either no change, or changes confined to lines outside 11-70. If the
diff touches the `SQL_TRANSLATION_SYSTEM_PROMPT` body, revert that file with
`git checkout -- text_to_sql_agent/llm.py` and re-check the per-file-ignores
entry from Task 1.

- [ ] **Step 5: Write Google-style docstrings for the public API**

For each remaining `D` finding in `text_to_sql_agent/`, expand the docstring to
the master §3 form. Ruff skips `_`-prefixed helpers automatically. Example, for
`execute_query` in `text_to_sql_agent/execution.py`:

```python
def execute_query(
    db_path: str,
    sql_string: str,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    progress_steps: int = DEFAULT_SQLITE_PROGRESS_STEPS,
) -> QueryResult:
    """Execute a validated SELECT against SQLite with read-only protections.

    Args:
        db_path: Filesystem path to the SQLite database.
        sql_string: A query already cleared by `is_safe_query`.
        max_rows: Maximum rows returned before the result is marked truncated.
        progress_steps: VM steps between progress-handler callbacks.

    Returns:
        A `QueryResult`. Its `error` is set to `RESULT_TRUNCATED_TO_<n>_ROWS`
        when more rows were available than `max_rows` allowed.

    Raises:
        ValueError: If `max_rows` is less than 1.
    """
```

Describe what the code already does. If writing the docstring reveals a bug,
note it in `docs/4_next_steps.md` for Phase 2 — do not fix it here.

- [ ] **Step 6: Verify lint is clean and behaviour is unchanged**

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Expected: no findings from either ruff command, and `19 passed`.

- [ ] **Step 7: Commit formatting separately from docstrings**

Two commits, so review can skim the mechanical one:

```bash
git status --short
git add text_to_sql_agent/config.py
git add -u
git commit -m "style: apply ruff format and autofixes across the repo

Mechanical only - no logic changed. Guards config.py's hand-aligned
STOPWORDS block with fmt: off so the formatter does not reflow it to one
token per line.

Verified: ruff format --check and ruff check both clean; uv run pytest
reports 19 passed. Confirmed by diff that SQL_TRANSLATION_SYSTEM_PROMPT
in llm.py is byte-identical - its long lines are prompt content, excluded
from E501 in pyproject.toml rather than rewrapped."

git add text_to_sql_agent/
git commit -m "docs(agent): expand public API docstrings to Google style

Brings the functions exported from text_to_sql_agent/__init__.py up to
master standard §3: Args, Returns, and Raises sections describing what
the code already does. Private helpers are untouched - ruff's pydocstyle
rules skip underscore-prefixed names.

No behaviour change. Verified: uv run pytest reports 19 passed."
```

---

### Task 4: Delete the duplicate shim

The highest-risk task. `text_to_sql_agent_mvp.py` is not a wrapper — its body is
byte-identical to `text_to_sql_agent/pipeline.py` apart from three docstrings.
The tests currently patch and exercise the shim's copy, which means
`pipeline.py`'s `ask_database` has **no coverage at all today**.

**Files:**
- Delete: `text_to_sql_agent_mvp.py`
- Modify: `app.py:23`, `scripts/evaluate_text_to_sql.py:15`, `tests/test_pipeline.py`, `tests/test_rag.py`, `tests/test_safety.py`, `tests/test_ingestion.py`, `tests/test_schema.py`, `tests/test_execution.py`, `text_to_sql_agent_mvp.ipynb`

**Interfaces:**
- Consumes: the split test files from Task 2.
- Produces: `text_to_sql_agent` is the only import path. Nothing references `text_to_sql_agent_mvp` afterwards.

- [ ] **Step 1: Prove the patch target must change, before changing it**

`pipeline.py` binds `generate_sql` at import time via `from .llm import
generate_sql`. Patching the *package* attribute therefore does not reach it.
Confirm that rather than trusting it:

```bash
uv run python -c "
import text_to_sql_agent as a, text_to_sql_agent.pipeline as p
print('package attr is pipeline attr:', a.generate_sql is p.generate_sql)
print('correct patch target: text_to_sql_agent.pipeline.generate_sql')
"
```

Expected: `True` for identity, but that identity is irrelevant — `patch.object`
rebinds a *name*, and the name `pipeline.generate_sql` is the one the call site
resolves. Patch `text_to_sql_agent.pipeline.generate_sql`.

- [ ] **Step 2: Repoint the six test files' imports**

In each of `tests/test_safety.py`, `tests/test_ingestion.py`,
`tests/test_schema.py`, `tests/test_rag.py`, `tests/test_execution.py`, and
`tests/test_pipeline.py`, replace:

```python
import text_to_sql_agent_mvp as agent
```

with:

```python
import text_to_sql_agent as agent
```

- [ ] **Step 3: Repoint the three patch targets in `tests/test_pipeline.py`**

`patch.object(agent, ...)` no longer reaches the call site. Replace each of the
three occurrences:

```python
with patch.object(agent, "generate_sql", side_effect=fake_generate_sql):
```

with:

```python
with patch("text_to_sql_agent.pipeline.generate_sql", side_effect=fake_generate_sql):
```

and likewise the two `return_value` forms:

```python
with patch("text_to_sql_agent.pipeline.generate_sql", return_value="DROP TABLE customers"):
...
with patch("text_to_sql_agent.pipeline.generate_sql", return_value="SELECT name FROM customers"):
```

- [ ] **Step 4: Run the tests — they now cover `pipeline.py` for the first time**

Run: `uv run pytest`
Expected: `19 passed`.

If `test_ask_database_executes_safe_generated_sql` fails with a real Gemini or
Ollama call attempt, the patch target is still wrong — the mock is not
intercepting. Re-read Step 3. This failure mode is the entire reason the shim
existed, so treat it as the expected error, not a surprise.

- [ ] **Step 5: Repoint the app and the evaluation script**

`app.py` line 23:

```python
import text_to_sql_agent as backend
```

Also update the module docstring on line 2, which names the shim:

```python
"""Streamlit UI for the Text-to-SQL agent (`text_to_sql_agent`)."""
```

`scripts/evaluate_text_to_sql.py` line 15:

```python
import text_to_sql_agent as agent
```

- [ ] **Step 6: Verify the app's whole API surface still resolves**

All twelve attributes were confirmed present in `__all__` on 2026-09-11. Re-run
the check rather than trusting the note:

```bash
uv run python -c "
import re, text_to_sql_agent as b
used = sorted(set(re.findall(r'backend\.([A-Za-z_]+)', open('app.py').read())))
missing = [n for n in used if not hasattr(b, n)]
print('used:', len(used), 'missing:', missing)
assert not missing, missing
print('APP API OK')
"
```

Expected: `used: 12 missing: []` then `APP API OK`.

- [ ] **Step 7: Update the notebook's import cell**

In `text_to_sql_agent_mvp.ipynb`, change the source line
`import text_to_sql_agent_mvp as agent` to `import text_to_sql_agent as agent`.

Then insert a new markdown cell at position 0 recording that outputs are a
snapshot — the notebook keeps its 11 cells of saved outputs as academic
evidence, which is a deliberate exemption from master §4:

```python
uv run python - <<'PY'
import json, pathlib
p = pathlib.Path("text_to_sql_agent_mvp.ipynb")
nb = json.loads(p.read_text())

for cell in nb["cells"]:
    cell["source"] = [
        line.replace("import text_to_sql_agent_mvp as agent", "import text_to_sql_agent as agent")
        for line in cell["source"]
    ]

note = {
    "cell_type": "markdown",
    "metadata": {},
    "source": [
        "> **Saved outputs are a point-in-time snapshot.**\n",
        "> They record a real run and are kept as evidence for the academic\n",
        "> deliverable; they are not a guarantee of current behaviour. Re-run the\n",
        "> notebook to reproduce. Import path updated to `text_to_sql_agent`\n",
        "> on 2026-09-11 when the compatibility shim was removed.\n",
    ],
}
if nb["cells"][0].get("source", [""])[0].startswith("> **Saved outputs"):
    nb["cells"][0] = note
else:
    nb["cells"].insert(0, note)

p.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n")
print("notebook updated:", len(nb["cells"]), "cells")
PY
```

- [ ] **Step 8: Verify the notebook JSON is still well-formed (master §10)**

```bash
uv run python -c "
import json; nb = json.load(open('text_to_sql_agent_mvp.ipynb'))
print('cells:', len(nb['cells']), 'with outputs:', sum(1 for c in nb['cells'] if c.get('outputs')))
assert 'text_to_sql_agent_mvp' not in json.dumps(nb)
print('NOTEBOOK OK')
"
```

Expected: `cells: 25 with outputs: 11` then `NOTEBOOK OK`.

- [ ] **Step 9: Delete the shim and confirm nothing references it**

```bash
git rm text_to_sql_agent_mvp.py
grep -rn "text_to_sql_agent_mvp" --include="*.py" --include="*.ipynb" --include="*.yml" . | grep -v "^./.git"
```

Expected: no output from grep except matches in `README.md` and
`docs/academic/report.md`, which Task 7 handles — those are `.md`, excluded by
the `--include` filters, so genuinely expect **zero** lines here.

- [ ] **Step 10: Full gate**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy text_to_sql_agent
uv run pytest
```

Expected: ruff silent, mypy `Success: no issues found`, pytest `19 passed`.

If mypy reports errors in `pipeline.py` that were previously hidden, fix only
annotations. If a fix would change behaviour, add
`# type: ignore[<code>]  # Phase 2` and record it in `docs/4_next_steps.md`.

- [ ] **Step 11: Commit**

```bash
git status --short
git add app.py scripts/evaluate_text_to_sql.py tests/ text_to_sql_agent_mvp.ipynb
git rm text_to_sql_agent_mvp.py
git commit -m "refactor: remove the duplicate compatibility shim

text_to_sql_agent_mvp.py was not a wrapper. Its body was byte-identical
to text_to_sql_agent/pipeline.py apart from three docstrings - about 170
duplicated lines of ask_database, ask_database_with_sql, ask_from_files
and _repair_sql. A fix applied to pipeline.py would not have reached the
code the tests ran.

It existed so patch.object(agent, 'generate_sql') would work: pipeline.py
binds the name at import via 'from .llm import generate_sql', so patching
the package attribute does not reach the call site. The three affected
tests now patch text_to_sql_agent.pipeline.generate_sql directly, which
removes the reason for the shim.

Net effect on coverage is positive: the four ask_database tests were
exercising the shim's copy, so pipeline.py had no coverage at all before
this commit.

The notebook keeps its saved outputs as academic evidence - a deliberate
exemption from master §4, now recorded in a dated cell at its top.

Verified: uv run pytest 19 passed; mypy clean; ruff clean; app.py's
twelve backend.* attributes all resolve against the package __all__;
notebook JSON well-formed at 25 cells with 11 outputs."
```

---

### Task 5: The packaging drift guard

**Files:**
- Create: `tests/test_packaging.py`

**Interfaces:**
- Consumes: `pyproject.toml` and `requirements.txt` from Task 1.
- Produces: the 20th test. Task 8's CI drift check is the second line of defence; this one fails locally and faster.

- [ ] **Step 1: Write the failing test**

Written to fail first: it will not pass if the header from Task 1 Step 4 is
missing, or if the two files disagree.

```python
"""Guards the generated requirements.txt against drifting from pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirement_names(lines: list[str]) -> set[str]:
    """Extract bare distribution names, dropping versions, markers, and extras."""
    names = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", stripped, maxsplit=1)[0]
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


def test_requirements_txt_is_marked_as_generated() -> None:
    header = (ROOT / "requirements.txt").read_text().splitlines()[0]
    assert "GENERATED" in header.upper(), (
        "requirements.txt must carry the generated-file header so it is not hand-edited"
    )


def test_requirements_txt_covers_every_runtime_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = _requirement_names(pyproject["project"]["dependencies"])
    exported = _requirement_names((ROOT / "requirements.txt").read_text().splitlines())

    missing = declared - exported
    assert not missing, (
        f"requirements.txt is stale, missing {sorted(missing)}. Regenerate with: "
        "uv export --no-hashes --no-dev --no-emit-project -o requirements.txt"
    )
```

`tomllib` is standard library from Python 3.11, which the Task 1 floor
guarantees. The assertion is one-directional — `uv export` resolves transitive
dependencies, so `exported` is legitimately a superset of `declared`. Asserting
set equality would fail on every transitive package.

- [ ] **Step 2: Run it and watch it pass, then prove it can fail**

```bash
uv run pytest tests/test_packaging.py -v
```

Expected: `2 passed`. A test that has never failed proves nothing, so break it
deliberately:

```bash
cp requirements.txt /tmp/req.bak
grep -v '^sqlglot' requirements.txt > /tmp/req.tmp && mv /tmp/req.tmp requirements.txt
uv run pytest tests/test_packaging.py -v
```

Expected: `test_requirements_txt_covers_every_runtime_dependency` FAILS with
`requirements.txt is stale, missing ['sqlglot']`.

- [ ] **Step 3: Restore and confirm green**

```bash
cp /tmp/req.bak requirements.txt
uv run pytest
```

Expected: `20 passed`.

- [ ] **Step 4: Commit**

```bash
git status --short
git add tests/test_packaging.py
git commit -m "test(packaging): fail when requirements.txt drifts from pyproject

requirements.txt is generated by uv export but must stay a real file at
the repo root, because Streamlit Cloud and the devcontainer both read it.
That makes silent drift possible whenever a dependency is added to
pyproject.toml without re-exporting.

Checks one direction only - uv export resolves transitive dependencies,
so the exported set is legitimately a superset of the declared one.

Verified: 20 passed; and confirmed the test actually fails by removing
sqlglot from requirements.txt, which produced the expected 'stale,
missing [sqlglot]' failure before restoring."
```

---

### Task 6: Agent instructions

Master §13 layer 2. Without these files no session in this repo loads the master
standard — the reason the repo drifted from it in the first place.

**Files:**
- Create: `AGENTS.md`, `CLAUDE.md`, `docs/0_coding_standards.md`

**Interfaces:**
- Consumes: the decisions recorded in the Phase 1 spec §3.
- Produces: `AGENTS.md` imports `@docs/0_coding_standards.md`; Task 7 links `docs/3_decisions.md` from it.

- [ ] **Step 1: Create `docs/0_coding_standards.md`**

Deltas only. Master §13 forbids restating the master — if a rule appears in both,
delete it here.

```markdown
# Project Coding Standards — aipa-text-to-sql-agent

The shared baseline is the master standard at
`~/Documents/GitHub/coding-standards/coding_standards.md`. This file records
**only** what is specific to this project or deliberately different. Per master
§13 it must never restate the master; if a rule here also appears there, delete
it here.

## 1. Doc shape

**Shape B** (app/product), per master §2. The repo's centre of gravity is the
Streamlit agent and its backend package. The offline evaluation harness measures
that agent rather than being the subject of the repo, and nothing is trained, so
Shape A's EDA-then-baseline sequence does not describe this work.

Renumbering an existing repo is normally forbidden by §2. It was done here on
2026-09-11 under the same section's exemption for "repos being substantially
reworked anyway" — see `docs/superpowers/specs/2026-09-10-refactor-roadmap.md`.

## 2. Deltas from the master

- **`line-length = 100`, not 79.** Master §3 asks for 79 "where practical". Here
  164 lines exceed 79 but only 43 exceed 100, and most of those in between are
  typed signatures and f-string report lines that read worse wrapped. Enforced by
  `ruff format`, so it is a ceiling and not a target.

- **`mypy --strict` over `text_to_sql_agent/`.** Neither the master standard nor
  any sibling project uses a type checker; master §3 asks for type hints without
  enforcing them. Adopted here because Phases 3 and 4 of the refactor rewrite
  that package heavily and strict typing pays for itself across a rewrite.
  `app.py`, `scripts/` and `tests/` are deliberately excluded.

- **No feature branches.** Single-owner project, so work commits directly to
  `tuannm3812/main-refinement` — this repo's default branch, not `main`. Master
  §10's pre-push workflow still applies in full. With no merge commits to
  summarise a phase, §9 commit hygiene carries the whole burden of a reviewable
  history.

- **`data/` exists deliberately.** Master §1 says to avoid it absent a real
  local-execution need. Six demo fixtures are tracked because `app.py` offers
  them as the built-in demo databases and `evaluation/cases.json` runs against
  them; the hosted demo is non-functional without them. `.gitignore` uses the
  master §8 ignore-then-negate pattern so the exception is visible rather than
  accidental. **Do not "tidy" this.**

- **The MVP notebook keeps its saved outputs.** Master §4 asks for outputs to be
  cleared or re-run when notebook code changes. `text_to_sql_agent_mvp.ipynb`
  keeps its 11 cells of output as point-in-time evidence for the academic
  deliverable in `docs/academic/`. A dated cell at the top says so. Any *code*
  change to the notebook still requires a re-run.

- **`E501` is off for `text_to_sql_agent/llm.py`.** Its long lines are all inside
  `SQL_TRANSLATION_SYSTEM_PROMPT`, where a line break is content the model reads.
  Rewrapping would change behaviour. Revisit if the prompt moves to its own file.

- **`requirements.txt` is generated**, by
  `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`. Edit
  `pyproject.toml` and re-export; never edit it directly. `tests/test_packaging.py`
  and CI both fail on drift. It cannot be deleted in favour of `pyproject.toml`
  alone because Streamlit Community Cloud and `.devcontainer/` read it.

## 3. Naming

- Retrieval results are `*RetrievalResult` dataclasses; schema units are `*Chunk`.
- Provider adapters are named for the provider (`gemini_manager.py`), not the
  vendor's model family.
- Error codes returned in `QueryResult.error` are `SCREAMING_SNAKE_CASE` string
  constants (`BLOCKED_UNSAFE_SQL`, `UNANSWERABLE_WITH_GIVEN_SCHEMA`), never
  free-form prose, because `app.py` and the evaluation harness branch on them.

## 4. Safety Conventions

- Generated SQL passes `is_safe_query` **and** executes under the read-only
  SQLite authorizer in `execution.py`. Neither is sufficient alone; never remove
  one because the other exists.
- The LLM receives schema metadata and low-cardinality value hints only. It never
  receives row data. Any change that would send rows to a provider needs an
  entry in `docs/3_decisions.md` first.
```

- [ ] **Step 2: Create `AGENTS.md`**

From `~/Documents/GitHub/coding-standards/templates/AGENTS.md.template`, 20-40
lines, referencing the master rather than duplicating it.

```markdown
# aipa-text-to-sql-agent

A Streamlit decision-support agent that translates natural-language questions
into safe, read-only SQLite queries, grounding the LLM with a local hybrid schema
RAG layer that indexes DDL and column metadata but never row data.

It is **not** a modelling repo — nothing is trained, and the `evaluation/`
harness measures the agent rather than a model. Do not confuse it with the
sibling `ai-meal-planner`, which is a FastAPI service.

## Standards

Follow the master standard at `~/Documents/GitHub/coding-standards/`.
Project-specific rules and deliberate overrides: @docs/0_coding_standards.md

## Deltas from the master

- `line-length = 100`, not 79 — see `docs/0_coding_standards.md` §2 for the count.
- `mypy --strict` over `text_to_sql_agent/` only. No sibling project uses a type
  checker; this one does because Phases 3-4 rewrite that package heavily.
- **No feature branches.** Commit directly to `tuannm3812/main-refinement`, which
  is this repo's default branch — not `main`.
- `data/` and the notebook's saved outputs are deliberate §8 and §4 exceptions.
  **Do not "tidy" either.**
- `requirements.txt` is **generated** by `uv export`. Edit `pyproject.toml` and
  re-export; CI and `tests/test_packaging.py` both fail on drift.

## Evidence locations

- `docs/3_decisions.md` — dated decision log; architectural claims trace here
- `docs/6_agent_log.md` — append-only record of agent work and what was verified
- `docs/superpowers/specs/` — the five-phase refactor roadmap and per-phase specs
- `evaluation/results/` — measured accuracy per provider and model

## Current state

- 2026-09-11: Phase 1 (foundation and standards alignment) complete. Baseline is
  20 passing tests; ruff, mypy and the packaging drift guard gate CI across
  Python 3.11-3.13. Phases 2-5 are specced in `docs/4_next_steps.md`.

## Open risks

- `safety.py`'s keyword regex falsely rejects SQLite's `REPLACE()` string
  function and any string literal containing `update`/`delete`/`create`. The
  sqlglot AST check beside it already enforces read-only correctly. Phase 2.
- `execution.py`'s progress handler returns a truthy value, so SQLite **aborts**
  any query exceeding 100,000 VM steps, surfacing as a bare
  `OperationalError: interrupted`. Intent unconfirmed. Phase 2.
- The RAG "embedding" in `rag.py` is a hashed bag of character n-grams, not a
  semantic embedding, and is recomputed per chunk per question with no cache. It
  largely duplicates the semantic signal beside it. Phase 4.
- Evaluation row-matching is duplicated between `app.py` and
  `scripts/evaluate_text_to_sql.py`. Phase 2.
```

- [ ] **Step 3: Create `CLAUDE.md`**

Exactly one line, so there is one source rather than two that drift:

```markdown
@AGENTS.md
```

- [ ] **Step 4: Verify the import path resolves**

`@docs/0_coding_standards.md` is only useful if the file is where it says:

```bash
test -f docs/0_coding_standards.md && test -f AGENTS.md && \
  [ "$(cat CLAUDE.md | tr -d '[:space:]')" = "@AGENTS.md" ] && echo "AGENT FILES OK"
```

Expected: `AGENT FILES OK`.

- [ ] **Step 5: Commit**

```bash
git status --short
git add AGENTS.md CLAUDE.md docs/0_coding_standards.md
git commit -m "docs(agents): add AGENTS.md, CLAUDE.md and project standards

The repo had neither file, so no session opened in it loaded the master
coding standard - which is how it drifted from the standard in the first
place. Adds master §13's layer 2: AGENTS.md holds repo identity, deltas,
evidence locations and open risks, and CLAUDE.md is one line importing
it so Codex and Claude Code read one source.

docs/0_coding_standards.md is layer 3 and records only deltas: the
100-character line length, mypy over the package, the trunk-based
workflow, and the deliberate §8 data/ and §4 notebook-output exceptions.
It does not restate the master, per §13.

The open-risks section names the four Phase 2 and Phase 4 defects so a
fresh session does not rediscover them.

Verified: all three files present; CLAUDE.md contains exactly @AGENTS.md."
```

---

### Task 7: Reshape docs to Shape B

**Files:**
- Move: `docs/supporting/architecture.md` → `docs/2_architecture.md`; `docs/supporting/deployment.md` → `docs/5_deployment.md`; `docs/supporting/screenshots/` → `docs/screenshots/`; `docs/supporting/architecture*.{drawio,excalidraw,png,jpeg,html}` → `docs/diagrams/`
- Create: `docs/1_brief.md`, `docs/3_decisions.md`, `docs/4_next_steps.md`, `docs/6_agent_log.md`
- Modify: `README.md`, `docs/academic/report.md`, `docs/2_architecture.md`

**Interfaces:**
- Consumes: `docs/0_coding_standards.md` from Task 6.
- Produces: the doc paths `AGENTS.md` already references. Task 8's CI does not depend on this task.

- [ ] **Step 1: Move files with `git mv` so history follows**

```bash
mkdir -p docs/diagrams
git mv docs/supporting/architecture.md docs/2_architecture.md
git mv docs/supporting/deployment.md docs/5_deployment.md
git mv docs/supporting/screenshots docs/screenshots
for f in docs/supporting/architecture*.drawio docs/supporting/architecture*.excalidraw \
         docs/supporting/architecture*.png docs/supporting/architecture*.jpeg \
         docs/supporting/architecture*.html; do
  [ -e "$f" ] && git mv "$f" docs/diagrams/
done
ls docs/supporting/ 2>/dev/null
```

Expected from the final `ls`: `screenshots.md` only. Merge its content into
`docs/2_architecture.md` as a "Screenshots" section, then
`git rm docs/supporting/screenshots.md` and `rmdir docs/supporting`.

- [ ] **Step 2: Find every link the move broke**

```bash
grep -rn "docs/supporting\|supporting/" --include="*.md" . | grep -v "^./docs/superpowers"
```

Fix each hit. Expected locations: `README.md` (screenshot table near the top, the
architecture image, and the deeper-diagram sentence), `docs/2_architecture.md`
(its own relative image paths), and `docs/academic/report.md`.

- [ ] **Step 3: Fix the three stale shim references**

`README.md:174` — the setup command:

```bash
python -c "import text_to_sql_agent as a; a.write_university_db('data/university_agent.db')"
```

`README.md:213` — the repo tree line. Delete the
`text_to_sql_agent_mvp.py` entry and add `pyproject.toml`, `AGENTS.md`, and
`CLAUDE.md` in their alphabetical positions.

`docs/academic/report.md:188` — delete the trailing sentence "The file
`text_to_sql_agent_mvp.py` remains as a compatibility wrapper for the notebook,
tests, and Streamlit app." and replace it with: "The Streamlit app, the notebook,
and the test suite all import the package directly."

- [ ] **Step 4: Fix the Python version claim**

`README.md` badge and prose say Python 3.10+. `pyproject.toml` now requires
`>=3.11,<3.14`. Change the badge URL to `Python-3.11%2B` and any prose to 3.11+.

- [ ] **Step 5: Write `docs/1_brief.md`**

```markdown
# Brief

**What:** A decision-support agent that answers plain-English questions about a
relational database by generating a single read-only SQLite `SELECT`, executing
it locally, and rendering the result.

**For whom:** A non-technical analyst who knows the business question but not the
schema, and a reviewer assessing whether an LLM can be trusted near a database.

**Done looks like:** A question in, a correct result table out, with the
generated SQL always visible and the retrieved schema explainable. The LLM never
receives row data — only DDL, column names, and low-cardinality value hints.

**Explicit non-goals:** No write path, ever. No cloud database. No fine-tuning.
No hiding the SQL from the user.

**Constraints:**
- SQLite only through Phase 2; the engine abstraction arrives in Phase 3.
- Two LLM backends: the Gemini API with multi-key failover, and local Ollama.
- The hosted Streamlit demo must stay within Community Cloud's free resources,
  which is why heavyweight embedding models are an optional dependency group
  rather than a runtime requirement.

Timestamped 2026-09-11. See `docs/superpowers/specs/2026-09-10-refactor-roadmap.md`
for the phased plan and `docs/4_next_steps.md` for what remains.
```

- [ ] **Step 6: Write `docs/3_decisions.md`**

Seed it with the decisions the spec already recorded, dated:

```markdown
# Decision Log

Newest first. Each entry states what was chosen and what it ruled out.

## 2026-09-11 — mypy strict over `text_to_sql_agent/`

**Chosen:** `mypy --strict` on the package only; `app.py`, `scripts/` and
`tests/` excluded.
**Ruled out:** No type checker, matching every sibling project and the master
standard, which asks for hints without enforcing them.
**Why:** Phases 3 and 4 rewrite the package heavily — an engine abstraction and a
RAG rewrite. Strict typing pays for itself across a rewrite of that size. The
excluded paths are either rewritten in Phase 2 anyway or not reusable API.

## 2026-09-11 — no feature branches

**Chosen:** Commit directly to `tuannm3812/main-refinement`.
**Ruled out:** Branch-per-phase with merge commits.
**Why:** Single-owner project; branch-and-merge buys review isolation that has no
second reviewer to serve. Consequence: master §9 commit hygiene now carries the
whole burden of a reviewable history, since no merge commit summarises a phase.

## 2026-09-11 — delete the MVP compatibility shim

**Chosen:** Delete `text_to_sql_agent_mvp.py`; tests patch
`text_to_sql_agent.pipeline.generate_sql` directly.
**Ruled out:** Keeping it as an import alias for the academic history.
**Why:** It was not an alias. Its body was byte-identical to `pipeline.py` apart
from three docstrings — about 170 duplicated lines — and the tests exercised
*its* copy, leaving `pipeline.py` with no coverage. A fix applied to
`pipeline.py` would not have reached the code under test.

## 2026-09-11 — `uv` with a generated `requirements.txt`

**Chosen:** `pyproject.toml` plus `uv.lock` as the source of truth;
`requirements.txt` generated by `uv export` and committed.
**Ruled out:** Dropping `requirements.txt` entirely; keeping it hand-maintained.
**Why:** Streamlit Community Cloud and `.devcontainer/devcontainer.json` both
read `requirements.txt`, so it must remain a real file at the root. Generating it
keeps one source of truth; `tests/test_packaging.py` and CI fail on drift.

## 2026-09-11 — Shape B doc numbering

**Chosen:** Renumber `docs/` to Shape B.
**Ruled out:** Leaving the ad hoc `academic/` and `supporting/` split, which
master §2 permits for existing repos.
**Why:** §2's exemption covers "repos being substantially reworked anyway", which
the five-phase roadmap is. Doing it now costs one commit; doing it after Phases
2-5 would churn far more links.
```

- [ ] **Step 7: Write `docs/4_next_steps.md`**

```markdown
# Next Steps

Phase 1 is complete. Phases 2-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Phase 2 — Correctness and seams (next)

1. **`safety.py` false positives.** The keyword regex rejects SQLite's
   `REPLACE()` string function and any string literal containing
   `update`/`delete`/`create`. The sqlglot AST check beside it already enforces
   read-only correctly, as does the authorizer in `execution.py`. Remove the
   regex; add regression tests for `REPLACE()` and for
   `WHERE note = 'please update'`.
2. **`execution.py` query aborts.** `set_progress_handler(lambda: 1, 100_000)`
   returns truthy, so SQLite aborts any query over 100,000 VM steps and surfaces
   a bare `OperationalError: interrupted`. Decide whether this is the intended
   runaway guard; either way make it explicit, typed, and covered.
3. **Deduplicate the evaluation harness.** Row-normalisation and matching are
   duplicated between `app.py` and `scripts/evaluate_text_to_sql.py`. Extract one
   module both import.
4. **Split `app.py`.** 736 lines, with a ~345-line `main()`. Extract to `ui/`
   modules behind a thin entrypoint.

## Phase 3 — Engine abstraction

Dialect/engine protocol behind `schema.py` and `execution.py`; SQLite becomes one
implementation, DuckDB the second, PostgreSQL third. Must precede Phase 4 —
schema chunking is engine-specific.

## Phase 4 — Real RAG

Decompose `retrieve_schema_context`'s 130-line body into named, individually
testable signal functions with weights in config. Replace the hashed
pseudo-embedding with real sentence embeddings behind an optional dependency
group, falling back to the lexical path so the hosted demo stays light. Persist
the index keyed by schema hash. Add recall@k and MRR to the evaluation harness so
the academic report's RAG claims become measurable.

## Phase 5 — Agent loop

plan → retrieve → generate → self-critique → execute → repair, with a structured
trace surfaced in the UI. The current single-shot repair becomes a degenerate
case.

## Deferred from Phase 1

Anything suppressed during Phase 1 with a `# noqa: ... # Phase 2` or
`# type: ignore[...] # Phase 2` comment. Find them with:
`grep -rn "Phase 2" --include="*.py" .`
```

- [ ] **Step 8: Write `docs/6_agent_log.md`**

Append-only from here on — master §13 says correct a past entry by adding a new
one, never by rewriting it.

```markdown
# Agent Collaboration Log

**Append-only.** Correct a past entry by adding a new one, never by rewriting it.
Superseded conclusions stay visible — when a claim later proves wrong, the trail
showing how it was reached is the useful part. Record what was *checked*, not
just what was claimed.

## 2026-09-11 — Phase 1: foundation and standards alignment

**Changed:** Added `pyproject.toml`, `uv.lock`, ruff/mypy/pytest config,
`AGENTS.md`, `CLAUDE.md`, `docs/0_coding_standards.md`. Split the 284-line test
file into seven per-module files plus `conftest.py`. Deleted
`text_to_sql_agent_mvp.py`. Renumbered `docs/` to Shape B. Rewrote CI as a
3.11-3.13 matrix.

**Verified:** `uv run pytest` 20 passed. `ruff check` and `ruff format --check`
clean. `mypy text_to_sql_agent` clean. `requirements.txt` installs into a clean
venv. `app.py`'s twelve `backend.*` attributes all resolve against the package
`__all__`. Notebook JSON well-formed, 25 cells, 11 with outputs. The packaging
drift guard was confirmed to fail when `sqlglot` was removed from
`requirements.txt`, then restored. Streamlit app booted locally and returned rows
for one query against the university demo database.

**Found, not fixed:** The shim was a byte-identical copy of `pipeline.py`, not a
wrapper — so `pipeline.py`'s `ask_database` had no test coverage before this
phase. Recorded as roadmap defect 11. The four Phase 2 and Phase 4 defects in
`safety.py`, `execution.py`, `rag.py` and the duplicated evaluation harness were
all left untouched by design; this phase changed no behaviour.

**Open:** Whether `execution.py`'s progress-handler abort is intentional. Nobody
has confirmed it; do not "fix" it in Phase 2 without deciding that first.
```

- [ ] **Step 9: Verify no dead links remain**

```bash
uv run python - <<'PY'
import pathlib, re
bad = []
for md in pathlib.Path("docs").rglob("*.md"):
    for target in re.findall(r"\]\((?!https?:|#)([^)]+)\)", md.read_text()):
        resolved = (md.parent / target.split("#")[0]).resolve()
        if not resolved.exists():
            bad.append(f"{md}: {target}")
for target in re.findall(r"\]\((?!https?:|#)([^)]+)\)", pathlib.Path("README.md").read_text()):
    if not (pathlib.Path(".") / target.split("#")[0]).exists():
        bad.append(f"README.md: {target}")
print("\n".join(bad) if bad else "ALL LINKS RESOLVE")
PY
```

Expected: `ALL LINKS RESOLVE`. Fix every listed path before committing.

- [ ] **Step 10: Commit**

```bash
git status --short
git add -A docs README.md
git commit -m "docs: renumber to Shape B and fix references left by the shim

Moves docs/supporting/ into the master §2 Shape B sequence: architecture
to 2_architecture.md, deployment to 5_deployment.md, screenshots and
diagrams to their own folders. Adds 1_brief.md, 3_decisions.md,
4_next_steps.md and the append-only 6_agent_log.md. docs/academic/ is
unchanged - it is the assessment deliverable.

Master §2 normally forbids renumbering an existing repo; this is taken
under its exemption for repos being substantially reworked, which the
five-phase roadmap is.

Also corrects three stale references to the deleted compatibility shim
(README setup command, README repo tree, report.md §188) and the Python
3.10 claim, which pyproject.toml now contradicts with >=3.11,<3.14.

Verified: every relative markdown link in README.md and docs/ resolves to
an existing file."
```

---

### Task 8: CI matrix

**Files:**
- Modify: `.github/workflows/tests.yml`, `.devcontainer/devcontainer.json`

**Interfaces:**
- Consumes: everything above. This is the gate that keeps it all true.
- Produces: nothing later depends on it.

- [ ] **Step 1: Rewrite the workflow**

Replaces `pip install -r requirements.txt` + `unittest` on a single version.

```yaml
name: Tests

on:
  push:
    branches: ["**"]
  pull_request:
    branches: ["**"]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --python ${{ matrix.python-version }}

      - name: Run tests
        run: uv run --python ${{ matrix.python-version }} pytest

  quality:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync

      - name: Lint
        run: uv run ruff check .

      - name: Format check
        run: uv run ruff format --check .

      - name: Type check
        run: uv run mypy text_to_sql_agent

      - name: requirements.txt is not stale
        run: |
          uv export --no-hashes --no-dev --no-emit-project -o /tmp/requirements.check
          tail -n +4 requirements.txt > /tmp/requirements.current
          diff -u /tmp/requirements.current /tmp/requirements.check
```

`fail-fast: false` so one version's failure does not hide the others. Lint, types
and the drift check live in a separate single-run `quality` job rather than
repeating three times across the matrix. The `tail -n +4` strips the three-line
generated-file header added in Task 1 Step 4 before diffing — if you changed the
header's line count, change this number to match.

- [ ] **Step 2: Verify the drift check locally before pushing**

CI failures on a workflow's first run are slow to debug. Run the exact commands:

```bash
uv export --no-hashes --no-dev --no-emit-project -o /tmp/requirements.check
tail -n +4 requirements.txt > /tmp/requirements.current
diff -u /tmp/requirements.current /tmp/requirements.check && echo "DRIFT CHECK OK"
```

Expected: `DRIFT CHECK OK` with no diff output. If the diff shows the header, the
`tail -n +4` offset is wrong — count the header lines and adjust.

- [ ] **Step 3: Point the devcontainer at uv**

In `.devcontainer/devcontainer.json`, replace `updateContentCommand` with:

```json
"updateContentCommand": "pip3 install --user uv && uv sync && echo '✅ Environment ready'",
```

and `postAttachCommand.server` with:

```json
"server": "uv run streamlit run app.py --server.enableCORS false --server.enableXsrfProtection false"
```

- [ ] **Step 4: Validate the JSON**

`devcontainer.json` permits comments, which `json.load` rejects — strip them
first rather than concluding the file is broken:

```bash
uv run python -c "
import json, re
raw = open('.devcontainer/devcontainer.json').read()
json.loads(re.sub(r'^\s*//.*$', '', raw, flags=re.M))
print('DEVCONTAINER JSON OK')
"
```

Expected: `DEVCONTAINER JSON OK`.

- [ ] **Step 5: Commit and push**

```bash
git status --short
git add .github/workflows/tests.yml .devcontainer/devcontainer.json
git commit -m "ci: run lint, types and tests on a 3.11-3.13 matrix via uv

Replaces a single-version pip + unittest run. Tests run on 3.11, 3.12 and
3.13 with fail-fast off so one version's failure does not mask another;
lint, mypy and the requirements.txt drift check run once in a separate
quality job rather than three redundant times.

The drift check re-exports from pyproject.toml and diffs against the
committed file, skipping its three-line generated header.

Points the devcontainer at uv sync so Codespaces matches local and CI.

Verified: the drift check commands run clean locally; devcontainer.json
parses after comment stripping."

git push
```

- [ ] **Step 6: Confirm CI is actually green**

Do not assume. Master §10 — never claim a run passed without reading its output:

```bash
sleep 45 && gh run list --limit 3
```

Expected: the newest run `completed  success`. If `gh` is unavailable, open the
Actions tab and read it. If any matrix entry fails, fix it before Task 9 — Task 9
records results, and recording an unverified pass is exactly the failure mode
master §13 warns about.

---

### Task 9: Verify the whole phase end to end

Nothing new is written here. This task exists because master §10 requires
verification proportional to the change, and the definition of done in the spec
lists commands nobody has yet run in sequence.

**Files:**
- Modify: `docs/6_agent_log.md` (only if a claim in it turns out to be wrong)

**Interfaces:**
- Consumes: everything.
- Produces: a verified phase.

- [ ] **Step 1: Run the spec's full definition-of-done gate**

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy text_to_sql_agent
uv run pytest
```

Expected, in order: sync succeeds; ruff silent; ruff silent; `Success: no issues
found`; `20 passed`.

- [ ] **Step 2: Confirm the shim is genuinely gone**

```bash
test ! -f text_to_sql_agent_mvp.py && echo "SHIM GONE"
grep -rn "text_to_sql_agent_mvp" . --exclude-dir=.git --exclude-dir=docs/superpowers | grep -v "\.ipynb:" || echo "NO REFERENCES"
```

Expected: `SHIM GONE`, then `NO REFERENCES`. The notebook is excluded because its
*filename* legitimately still contains that string; its cell contents were
checked in Task 4 Step 8.

- [ ] **Step 3: Boot the app and run a real query**

The one check no automated test covers, and the spec's main risk.

```bash
uv run streamlit run app.py --server.headless true --server.port 8501
```

In the browser at `http://localhost:8501`: select the university demo database,
ask "how many students are enrolled in each course?", and confirm a result table
renders with the generated SQL visible below it. Stop the server with Ctrl-C.

If it fails at import, Task 4 Step 6 missed an attribute. If it fails at query
time with a missing API key, that is expected without `GEMINI_API_KEY` — set one,
or switch the provider to Ollama, and retry. A key error is not a Phase 1
regression.

- [ ] **Step 4: Reconcile the log against what actually happened**

Re-read the "Verified" paragraph in `docs/6_agent_log.md` written in Task 7. Any
claim there that did not actually happen — a check skipped, a count different
from 20, the app not booted — gets corrected by **appending a new dated entry**,
never by editing that one. Master §13: an unverified finding repeated across
handoffs hardens into fact.

- [ ] **Step 5: Final commit if anything changed**

```bash
git status --short
```

If clean, the phase is done — nothing to commit. If the log needed a correction:

```bash
git add docs/6_agent_log.md
git commit -m "docs(log): append Phase 1 verification corrections

Records what the end-to-end gate actually produced, where it differed
from what the Phase 1 entry claimed. Appended rather than edited, per
master §13."
git push
```

---

## Self-Review

**Spec coverage.** Every section of the Phase 1 spec maps to a task: §5.1
packaging → Task 1; §5.2 ruff → Tasks 1 and 3; §5.3 agent instructions → Task 6;
§5.4 mypy → Tasks 1 and 4 Step 10; §5.5 shim removal → Task 4; §5.6 tests →
Tasks 2 and 5; §5.7 CI → Task 8; §5.8 git hygiene → Task 1 Steps 6-8; §5.9 docs →
Task 7. The spec's definition of done is Task 9.

**Ordering.** Tooling precedes source changes so every later task runs the same
gate. The test split (Task 2) precedes shim deletion (Task 4) so the riskiest
change fails in one named file. Docs (Task 7) follow shim deletion because they
must describe the final state. CI (Task 8) is last because it asserts everything
above.

**Type consistency.** The three fixture names — `customers_db`,
`customers_courses_db` and `customers_sales_courses_db` — are defined in Task 2
Step 2 and used unchanged in Task 2 Steps 5-9. All return `str`, matching the
`str` parameters the agent functions take. The patch target string
`"text_to_sql_agent.pipeline.generate_sql"` is identical across all three uses in
Task 4 Step 3.

**Known plan-level risk.** Task 1 Step 2 may fail to resolve `sqlglot<28` if the
current release has moved past it; the step says to widen that one bound rather
than remove bounds wholesale. Task 8's `tail -n +4` is coupled to the exact
header written in Task 1 Step 4 — both steps say so.
