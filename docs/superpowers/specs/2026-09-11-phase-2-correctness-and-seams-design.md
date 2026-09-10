# Phase 2 — Correctness and Seams

**Date:** 2026-09-11
**Status:** Awaiting review
**Parent:** `2026-09-10-refactor-roadmap.md`
**Addresses:** Roadmap defects 1, 2, 5, 6, plus the two gaps the Phase 1 final review surfaced
**Baseline standard:** `~/Documents/GitHub/coding-standards/coding_standards.md`

## 1. Purpose

Phase 1 changed no behaviour by design. Phase 2 is where behaviour changes: it
fixes the two real correctness defects in the safety and execution layers,
removes the duplicated evaluation logic, and splits the 759-line `app.py` into
modules with boundaries that Phases 3-5 can build against.

Unlike Phase 1, **this phase deliberately changes what the code does.** Every
such change ships with a test that fails before it and passes after.

## 2. Non-goals

- No engine abstraction. SQLite stays the only backend; that is Phase 3.
- No RAG scoring changes. `rag.py` is not touched beyond what the `app.py` split
  requires; that is Phase 4.
- No agent loop. The single-shot repair in `pipeline.py` stays as it is; that is
  Phase 5.
- No new LLM providers, no UI redesign, no new user-facing features.

## 3. Findings this phase acts on

Every claim below was reproduced on 2026-09-11 against the current tree. The
commands are in §9 so a reviewer can re-run them.

### 3.1 `is_safe_query` rejects legitimate SQL — and its regex is load-bearing

`text_to_sql_agent/safety.py` screens SQL with a keyword regex before the
sqlglot AST check. The regex matches keywords **anywhere** in the statement,
including inside string literals and function names:

| SQL | Current verdict | Correct verdict |
|---|---|---|
| `SELECT REPLACE(name,'a','b') FROM customers` | **rejected** | allow — `REPLACE` is a standard SQLite string function |
| `SELECT * FROM orders WHERE note = 'please update'` | **rejected** | allow |
| `SELECT * FROM orders WHERE status = 'delete'` | **rejected** | allow |
| `SELECT * FROM t WHERE action = 'create'` | **rejected** | allow |

Four legitimate read-only queries the model can plausibly generate are blocked,
and the user sees `BLOCKED_UNSAFE_SQL` with no way to tell it was a false alarm.

**The non-obvious part.** The intuitive fix — delete the regex, keep the AST
check — opens a hole. Verified: with the regex removed, `SELECT 1; DROP TABLE t`
and `SELECT * FROM t; DELETE FROM t;` both **pass**. `sqlglot.parse_one` parses
only the first statement, so `_is_safe_ast` inspects a harmless `SELECT` and
approves a payload it never looked at. The regex is currently the only thing
catching stacked statements, entirely by accident.

So the regex cannot simply be removed; it must be **replaced by an explicit
multi-statement guard**. Verified proposal: parse with `sqlglot.parse` (plural,
which returns every statement) and reject when the count is not exactly 1.
Against 14 dangerous and 7 legitimate inputs this produced **zero leaks and zero
false rejects**, including `SELECT * FROM t WHERE x='a;b'` — a semicolon inside a
string literal, which any naive `split(";")` would break.

### 3.2 The query-abort guard is real, but reports as a crash

`execution.py` sets `conn.set_progress_handler(lambda: 1, progress_steps)` with
`DEFAULT_SQLITE_PROGRESS_STEPS = 100_000`. A SQLite progress handler that returns
non-zero **aborts the query**, so this is a runaway-query killer.

It is genuinely useful and should be kept. Verified: a self-join over 60,000 rows
is aborted, while **all 12 gold evaluation cases pass untouched** — the demo
databases are small (largest is `retail_analytics.db` at 11 tables / 1,851 rows),
so the threshold only bites on pathological queries. On a hosted demo anyone can
type into, that protection matters.

The defect is not the guard, it is the reporting. The abort surfaces as a bare
`sqlite3.OperationalError: interrupted` propagating out of `execute_query`, which:

- `ask_database` catches and stringifies into `QueryResult.error` as
  `"OperationalError: interrupted"` — meaningless to a user;
- worse, `ask_database` treats it as a *generation* failure and spends an LLM
  call trying to "repair" SQL that was never wrong.

### 3.3 Evaluation logic is duplicated verbatim

`_normalise_rows`, `_canonical_value` and `_value_rows_match` exist identically in
`app.py:262-280` and `scripts/evaluate_text_to_sql.py:25-43`, differing only in
type annotations. `_load_evaluation_cases` and the case-running loop are near
duplicates too. Any change to how a generated result is compared against gold
must be made twice or the app and the CLI silently disagree about accuracy.

### 3.4 `app.py` is 759 lines with a 367-line `main()`

`main()` covers secrets, uploads, the sidebar, the chat loop, chart heuristics
and an evaluation tab in one function. It is the file Phase 5 must add a trace
view to, and the one no gate currently checks (§3.5).

### 3.5 Two gaps the Phase 1 final review surfaced

- **`app.py` is not type-checked.** `pyproject.toml` sets
  `files = ["text_to_sql_agent"]`, and ruff does not flag bad module-attribute
  access. A rename in `__init__.py`'s `__all__` would leave `backend.<old_name>`
  rotting in `app.py` with ruff, mypy and pytest all green, and the app raising
  `AttributeError` on the user's first query. `mypy app.py` currently reports
  about 20 errors.
- **`ask_database_with_sql`, `ask_from_files` and `_repair_sql` have no tests.**
  `ask_database_with_sql` is the function `app.py` actually calls, and
  `ask_from_files` is the CSV-upload path.

## 4. Design

### 4.1 Safety (`text_to_sql_agent/safety.py`)

Replace the keyword regex with a structural check. The final ordering:

1. reject empty input;
2. require the statement to start with `SELECT` or `WITH` (cheap, keeps the
   error early and readable);
3. reject any reference to `sqlite_master` / `sqlite_schema`;
4. parse with `sqlglot.parse(s, read="sqlite")` and **reject unless exactly one
   statement** — this is the new guard, and it replaces the regex's accidental
   protection with a deliberate one;
5. run the existing `_is_safe_ast` forbidden-node check.

`_DANGEROUS_SQL_PATTERN` is deleted. Nothing else in the module changes.

**The sqlglot import is optional.** `safety.py` guards it with `try/except
ModuleNotFoundError` and `_is_safe_ast` returns `True` when sqlglot is absent —
i.e. it fails **open**. Removing the regex makes that fallback materially weaker,
because the regex was the only non-sqlglot defence. sqlglot is a hard runtime
dependency in `pyproject.toml`, so the fallback exists only for an environment
that cannot occur. **Change it to fail closed**: when sqlglot is unavailable,
`is_safe_query` returns `False` and the caller reports that SQL validation is
unavailable. Refusing to run is the correct failure mode for a safety check.

This is defence in depth, not the only defence: `execution.py`'s read-only
connection and authorizer still block writes independently, and neither is
touched here.

### 4.2 Execution (`text_to_sql_agent/execution.py`)

Keep the guard; make it legible.

- Rename `DEFAULT_SQLITE_PROGRESS_STEPS` to `DEFAULT_MAX_VM_STEPS` and document
  what it does, since "progress steps" describes the mechanism rather than the
  intent.
- Replace the bare `lambda: 1` with a named function whose docstring says that
  returning non-zero aborts the query.
- Catch the resulting `sqlite3.OperationalError` inside `execute_query`,
  distinguish the interrupt from other operational errors, and return
  `QueryResult(columns=[], rows=[], sql=sql, error=f"QUERY_ABORTED_AFTER_{n}_VM_STEPS")`
  rather than letting it propagate.

`QUERY_ABORTED_AFTER_<n>_VM_STEPS` joins `BLOCKED_UNSAFE_SQL` and
`UNANSWERABLE_WITH_GIVEN_SCHEMA` as a `SCREAMING_SNAKE_CASE` error code, matching
the convention `docs/0_coding_standards.md` §3 already records.

**`pipeline.py` must not try to repair an aborted query.** Today any exception
from `execute_query` triggers `_repair_sql` and an LLM call. Once the abort is a
returned error rather than a raised one, the repair path is no longer reached for
it — that is the point, and a test asserts `generate_sql` is called exactly once
for an aborted query.

`app.py` renders the new code with a human-readable message.

### 4.3 Shared evaluation module

Create `text_to_sql_agent/evaluation.py` holding the single copy of:

- `normalise_rows`, `canonical_value`, `rows_match` (public — they are the
  comparison contract, and the leading underscores were the only reason two
  copies could drift unnoticed);
- `load_cases(path)`;
- `EvaluationCase` and `CaseOutcome` dataclasses;
- `run_case(...)` returning a `CaseOutcome`.

`app.py` and `scripts/evaluate_text_to_sql.py` both import it. Neither keeps a
local copy. It lives inside the package rather than in the `evaluation/`
directory because that directory holds *data* (`cases.json`, `results/`), and
because `pyproject.toml` packages `text_to_sql_agent` only.

Row comparison gets its own tests — it currently has none in either location,
despite being what every reported accuracy number depends on.

### 4.4 Splitting `app.py`

`app.py` becomes a thin entrypoint. Streamlit Cloud is configured to run
`app.py`, so **the filename and its `main()` entrypoint must not move.**

| New module | Holds |
|---|---|
| `ui/constants.py` | `GEMINI_MODELS`, `OLLAMA_MODELS`, `DEMO_DATABASES` |
| `ui/secrets.py` | `_streamlit_secret`, `_active_gemini_key`, `_model_name_for_provider` |
| `ui/uploads.py` | `_write_uploaded_db`, `_write_uploaded_csvs`, `_active_db_path` |
| `ui/results.py` | `_result_to_dataframe`, `_chartable_columns`, `_render_assistant_turn` |
| `ui/sidebar.py` | the sidebar block, returning a settings dataclass |
| `ui/chat.py` | the chat loop |
| `ui/evaluation.py` | the evaluation expander, calling `text_to_sql_agent.evaluation` |

The sidebar currently communicates through `st.session_state` string keys read
elsewhere in `main()`. Extracting it as-is would leave that coupling invisible
across module boundaries. It returns a frozen `Settings` dataclass
(`provider`, `model_name`, `use_rag`, `rag_top_k`, `db_path`, `api_key`) instead,
so the chat and evaluation modules take an argument rather than reaching into
global state. `st.session_state` remains only for what it is for: values that
must survive a rerun, such as chat history.

`ui/` sits at the repo root beside `app.py`, not inside `text_to_sql_agent/`.
The backend package is a reusable library that Phases 3-5 extend and that the
notebook and evaluation script import; Streamlit presentation code does not
belong in it. The cost is that `pyproject.toml`'s
`[tool.hatch.build.targets.wheel] packages = ["text_to_sql_agent"]` does not
ship `ui/` in the wheel. That is correct and deliberate — nothing installs this
project as a wheel to run the UI; Streamlit Cloud and the devcontainer both run
`app.py` from a checkout. Do **not** "fix" this by adding `ui` to the wheel
packages: that would publish a Streamlit-dependent module from a library whose
consumers may not have Streamlit installed. Record the reasoning in
`docs/0_coding_standards.md`.

**This is a refactor with no visible change.** The app must look and behave
identically; §6 says how that is checked.

### 4.5 Type-check `app.py` and `scripts/`

Extend mypy's `files` to `["text_to_sql_agent", "app.py", "ui", "scripts"]` and
fix what it reports. The Phase 1 measurement of ~20 errors was against the
current monolithic `app.py`; the split changes the count, so it is re-measured
rather than assumed.

Strictness is deliberately **not** uniform: `text_to_sql_agent` keeps
`strict = true`; `app.py`, `ui/` and `scripts/` get default settings with
`ignore_missing_imports = true`, because Streamlit's decorated API produces
`no-any-return` and `type-arg` noise that strict mode cannot resolve without
casts that document nothing. Recorded in `docs/0_coding_standards.md`.

CI's `quality` job runs plain `uv run mypy`, picking up the configured file list.

### 4.6 Missing pipeline tests

Add to `tests/test_pipeline.py`:

- `ask_database_with_sql` returns the generated SQL alongside the result;
- `ask_database_with_sql` returns `("", QueryResult(error=...))` when generation
  raises;
- `ask_from_files` routes a `.db` path to `ask_database`;
- `ask_from_files` ingests CSVs then queries the resulting database;
- `ask_from_files` rejects mixed extensions and an empty list;
- `_repair_sql` is invoked once when execution fails and its repaired SQL is
  re-checked by `is_safe_query` before running;
- `_repair_sql` is **not** invoked for an aborted query (§4.2).

## 5. Order of work

1. Safety fix (self-contained, highest value, tests first).
2. Execution fix (touches `pipeline.py`'s repair path).
3. Pipeline tests (cover the functions before §4.4 moves their callers).
4. Shared evaluation module.
5. `app.py` split.
6. mypy over the UI, last, since §4.4 changes what it must check.

## 6. Risks

**The safety change is a security change.** Widening what `is_safe_query`
accepts is the one place this phase could do real harm. Mitigation: the test
suite gains an explicit corpus of dangerous inputs — stacked statements, CTEs
wrapping a `DELETE`, `PRAGMA`, `ATTACH`, `sqlite_master`, and comment-obfuscated
variants — asserted to be rejected, plus the legitimate corpus asserted accepted.
The §3.1 table is the starting point, not the whole corpus. The read-only
connection and authorizer in `execution.py` are not modified, so SQLite itself
remains a second, independent line of defence.

**The `app.py` split changes no behaviour but has no test to prove it.** There is
no UI test, and adding a Streamlit test harness is out of scope. Mitigation: the
split is mechanical — move code, do not edit it — and lands as its own commits,
separate from any logic change, so a bisect can isolate it. Verification is the
scripted import-and-query check from Phase 1's Task 9 plus a manual pass over the
running app against a written checklist: each demo database loads, a CSV upload
ingests, a question returns a table, the SQL expander shows SQL, the RAG report
renders, and the evaluation tab runs.

**`QUERY_ABORTED_AFTER_<n>_VM_STEPS` is a new error string.** `app.py` and the
evaluation harness branch on error codes. Every consumer must be updated in the
same commit that introduces it, and a test asserts the app's renderer has a
message for each known code.

**Streamlit Cloud must keep working.** `app.py` stays the entrypoint, and the new
`ui/` package must be included in the deployed tree — it is a plain directory in
the repo root, so it is, but the hosted demo is checked after merge rather than
assumed.

## 7. Definition of done

```
uv run ruff check .
uv run ruff format --check .
uv run mypy                       # now covers app.py, ui/ and scripts/
uv run pytest                     # 20 existing + the new safety/execution/pipeline/evaluation tests
uv run python -c "import app"
uv run streamlit run app.py       # manual checklist in §6
```

CI green on 3.11-3.13. `app.py` under 100 lines. No `_normalise_rows`,
`_canonical_value` or `_value_rows_match` outside `text_to_sql_agent/evaluation.py`.
`docs/3_decisions.md` records the safety and fail-closed decisions with dates,
`docs/4_next_steps.md` drops what this phase completed, and `docs/6_agent_log.md`
gains an entry recording what was verified — including anything that was not.

## 8. Out of scope, deliberately

- `rag.py`'s scoring, caching, and 149-line `retrieve_schema_context`. Phase 4.
- The `seed` parameter of `create_dummy_university_data`, which is accepted and
  ignored. Harmless; note it in `docs/4_next_steps.md` rather than fixing it here.
- A Streamlit UI test harness.

## 9. Reproducing the findings

```bash
# 3.1 false positives, and the stacked-statement hole a naive fix opens
uv run python -c "
from text_to_sql_agent import is_safe_query as s
for q in [\"SELECT REPLACE(name,'a','b') FROM customers\",
          \"SELECT * FROM orders WHERE note = 'please update'\"]:
    print(s(q), q)"

# 3.2 the abort, and that no gold case trips it
uv run python -c "
import json; from text_to_sql_agent import execute_query
for c in json.load(open('evaluation/cases.json')):
    print(len(execute_query(c['db_path'], c['gold_sql']).rows), c['question'][:40])"

# 3.3 the duplication
diff <(sed -n '262,280p' app.py) <(sed -n '25,43p' scripts/evaluate_text_to_sql.py)

# 3.4 the size
wc -l app.py
```
