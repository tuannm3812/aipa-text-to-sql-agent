# Next Steps

Phases 1, 2, and 3a are complete. Phases 3b-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Phase 3b — PostgreSQL (next)

Third `Engine` implementation, added to the `text_to_sql_agent/engines/`
package Phase 3a built. It inherits the reachability, schema-dispatch, and
dialect-prompt plumbing Phase 3a landed ahead of DuckDB for exactly this
reason, and must pass the same `tests/test_engine_conformance.py` suite with
no changes to the suite's assertions. Per `docs/3_decisions.md`, there is no
shared read-only mechanism to reuse from SQLite or DuckDB — PostgreSQL proves
its own guarantee, most likely via a read-only transaction, and the
conformance suite is what holds it to the same bar. Must precede Phase 4 —
schema chunking is engine-specific.

Carried over from Phase 3a, closed out or newly found:

1. **`ui/chat.py` does not catch an exception from `ask_database_with_sql`.**
   If a database becomes unreachable between the sidebar's check and the
   question being asked, Streamlit renders the raw traceback and
   `redact_dsn` never sees that text. Harmless today — no current engine's
   error text contains a password — but it must be closed before a
   PostgreSQL driver error (which commonly echoes the DSN it failed to
   reach) can land on the page unredacted. **New in Phase 3a, important for
   3b.**
2. **The schema cache keys on the raw DSN, not a normalised one.** A
   relative and an absolute path to the same SQLite/DuckDB file produce two
   separate `lru_cache` entries under `(dsn, fingerprint)` keying (see
   `docs/3_decisions.md`), since nothing canonicalises the DSN string before
   it becomes half the cache key. Low impact today; worth resolving before a
   PostgreSQL DSN's equivalent aliasing (e.g. host vs. `127.0.0.1`) makes it
   worse.
3. **Value hints can go stale on a coarse-mtime filesystem.** Predates Phase
   3a.
4. **Schema-qualified table identity, decided once for both engines.** DuckDB
   is currently scoped to the `main` schema across every layer (see
   `docs/3_decisions.md`, 2026-09-25); a DuckDB database whose tables live
   outside `main` presents an empty schema and answers
   `UNANSWERABLE_WITH_GIVEN_SCHEMA`. PostgreSQL cannot take the same
   shortcut for long — schemas are intrinsic there, `public` is merely the
   default, and multi-schema databases are normal — so 3b is where the
   qualified identity should be carried through DDL, chunks, value hints,
   foreign keys, `table_names()` and safety validation, for both engines at
   once. Changing one layer alone leaves the contract inconsistent, which is
   the exact defect the 2026-09-25 fix closed.
5. **`schema_fingerprint()` is file-level, not schema-scoped.** For DuckDB it
   is `(path, mtime_ns, size)` over the whole file, so adding or dropping a
   table outside `main` moves the schema-chunks cache key even though what is
   advertised to the model cannot change. Measured, not fixed: the cost is a
   redundant recompute, never a wrong result. Worth folding into the
   qualified-identity work above rather than fixing alone.

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
