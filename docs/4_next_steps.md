# Next Steps

Phase 1 is complete. Phases 2-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Phase 2 — Correctness and seams (next)

1. **`app.py` is not type-checked.** `pyproject.toml` sets
   `files = ["text_to_sql_agent"]`, so `mypy` never sees `app.py`, and `ruff`
   does not catch bad module-attribute access on it either. A Phase 2 rename
   in `__init__.py`'s `__all__` would leave `backend.<old_name>` rotting in
   `app.py` with every gate green, and the app would raise `AttributeError`
   on the user's first query. `mypy app.py` currently reports about 20
   errors, so closing this is real work, not a one-line config change.
2. **`ask_database_with_sql` and `ask_from_files` have no tests.**
   `tests/test_pipeline.py` covers only `ask_database`, yet
   `ask_database_with_sql` is the function `app.py` actually calls, and
   `ask_from_files` is the CSV-upload path. `_repair_sql` is untested too.
3. **`safety.py` false positives.** The keyword regex rejects SQLite's
   `REPLACE()` string function and any string literal containing
   `update`/`delete`/`create`. The sqlglot AST check beside it already enforces
   read-only correctly, as does the authorizer in `execution.py`. Remove the
   regex; add regression tests for `REPLACE()` and for
   `WHERE note = 'please update'`.
4. **`execution.py` query aborts.** `conn.set_progress_handler(lambda: 1,
   progress_steps)`, with `progress_steps` defaulting to 100,000, returns
   truthy, so SQLite aborts any query over 100,000 VM steps and surfaces a
   bare `OperationalError: interrupted`. Decide whether this is the intended
   runaway guard; either way make it explicit, typed, and covered.
5. **Deduplicate the evaluation harness.** Row-normalisation and matching are
   duplicated between `app.py` and `scripts/evaluate_text_to_sql.py`. Extract one
   module both import.
6. **Split `app.py`.** 759 lines, with a 367-line `main()`. Extract to `ui/`
   modules behind a thin entrypoint.

## Phase 3 — Engine abstraction

Dialect/engine protocol behind `schema.py` and `execution.py`; SQLite becomes one
implementation, DuckDB the second, PostgreSQL third. Must precede Phase 4 —
schema chunking is engine-specific.

## Phase 4 — Real RAG

Decompose `retrieve_schema_context`'s 179-line body into named, individually
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

Nothing was actually deferred with a `# noqa: ... # Phase 2` or
`# type: ignore[...] # Phase 2` marker comment during Phase 1 — `grep -rn
"Phase 2" --include="*.py" .` returns no matches. This section is kept as a
placeholder in case a future phase leaves one; treat an empty grep result as
"nothing deferred," not as a hidden backlog.
