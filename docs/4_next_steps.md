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
