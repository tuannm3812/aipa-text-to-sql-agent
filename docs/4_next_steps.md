# Next Steps

Phases 1 and 2 are complete. Phases 3-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Phase 3 — Engine abstraction (next)

Dialect/engine protocol behind `schema.py` and `execution.py`; SQLite becomes one
implementation, DuckDB the second, PostgreSQL third. Must precede Phase 4 —
schema chunking is engine-specific.

Phase 2 left two seams this will build on: `execution.py` now returns typed
error codes rather than raising, so a second engine has a contract to implement
rather than an exception shape to imitate; and `is_safe_query` is SQLite-specific
today (its internals check keys off `sqlite_`/`pragma_` names), so the safety
layer needs a per-dialect story before a second engine lands.

## Phase 4 — Real RAG

Decompose `retrieve_schema_context`'s 182-line body into named, individually
testable signal functions with weights in config. Replace the hashed
pseudo-embedding with real sentence embeddings behind an optional dependency
group, falling back to the lexical path so the hosted demo stays light. Persist
the index keyed by schema hash. Add recall@k and MRR to the evaluation harness so
the academic report's RAG claims become measurable.

## Phase 5 — Agent loop

plan → retrieve → generate → self-critique → execute → repair, with a structured
trace surfaced in the UI. The current single-shot repair becomes a degenerate
case.

## Smaller items, not tied to a phase

1. **`safety.py` rejects a leading comment or BOM.** `/* report */ SELECT 1`,
   `-- report\nSELECT 1` and a BOM-prefixed `SELECT 1` are all refused, because
   `_ALLOWED_PREFIX` anchors on the first character and `.strip()` does not
   remove U+FEFF. Low impact in the main path — `_extract_sql_from_text` in
   `llm.py` re-anchors model output at the first `SELECT`/`WITH` — but reachable
   wherever raw SQL is fed in directly, such as the evaluation harness. Found by
   adversarial review during Phase 2 and deliberately left; fix by stripping
   leading comments and a BOM before the prefix check.

2. **`Settings.gemini_key` has no consumers.** `ui/settings.py` carries the
   field, but nothing outside `ui/sidebar.py` reads it — `key_ok` is what the
   chat path actually uses. Drop the field, or give it a consumer.

3. **`create_dummy_university_data`'s `seed` argument is inert.** It is accepted
   and ignored; the docstring says so. Either make it seed the generator or
   remove it.

## Deferred markers

Nothing is currently deferred with a `# noqa: ... # Phase N` or
`# type: ignore[...] # Phase N` marker comment. Check with
`grep -rn "Phase [0-9]" --include="*.py" .` — treat an empty result as
"nothing deferred", not as a hidden backlog.
