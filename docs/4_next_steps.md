# Next Steps

Phases 1, 2, 3a, and 3b are complete. Phases 4-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Phase 4 — Real RAG (next)

Decompose `retrieve_schema_context`'s 182-line body into named, individually
testable signal functions with weights in config. Replace the hashed
pseudo-embedding with real sentence embeddings behind an optional dependency
group, falling back to the lexical path so the hosted demo stays light. Persist
the index keyed by schema hash. Add recall@k and MRR to the evaluation harness so
the academic report's RAG claims become measurable.

Carried over from Phase 3b, still open:

1. **The schema cache keys on the raw DSN, not a normalised one.** A relative
   and an absolute path to the same SQLite/DuckDB file, or two equivalent
   spellings of the same PostgreSQL host (`localhost` vs `127.0.0.1`), still
   produce two separate `lru_cache` entries under `(dsn, fingerprint)` keying
   (see `docs/3_decisions.md`), since nothing canonicalises the DSN string
   before it becomes half the cache key. Low impact today — a wrong cache
   entry recomputes rather than serves stale data, because the fingerprint
   still has to match — but worth resolving alongside any RAG-layer caching
   work in this phase.
2. **Value hints can go stale on a coarse-mtime filesystem**, for SQLite and
   DuckDB specifically — both fingerprint by file `(path, mtime_ns, size)`,
   which some filesystems report at whole-second resolution. PostgreSQL does
   not have this problem: its `schema_fingerprint()` hashes a live catalogue
   read instead of a file stat (`docs/3_decisions.md`, 2026-09-26), at the
   cost of one catalogue query per cache check. Predates Phase 3a.
3. **`PostgresEngine.raw_schema()` loses column type precision.**
   `information_schema.columns.data_type` reports the bare type name
   (`numeric`, not `numeric(10,2)`); the synthesised `CREATE TABLE` DDL sent
   to the model is therefore less precise than the server's real schema.
   Low impact today — nothing in the demo corpus depends on numeric
   precision for correct SQL generation — but worth fixing if a future
   evaluation case needs it (`information_schema.columns.numeric_precision`/
   `numeric_scale` carry the missing detail).
4. **`execution.execute_query`'s `max_vm_steps` parameter is misleadingly
   named for DuckDB and PostgreSQL.** Both hold a millisecond budget there,
   not a VM-instruction count; the name is documented as back-compatible in
   the parameter's own docstring, but a rename (with the SQLite call sites
   updated too) would remove the need for that caveat.
5. **The PostgreSQL conformance fixture's cleanup only runs at test setup.**
   `tests/test_engine_conformance.py` drops any stray table before each
   PostgreSQL test starts, so a real read-only bypass still fails loudly in
   its own test, but leaves no artifact for the next run to inspect — unlike
   SQLite/DuckDB, which get a fresh `tmp_path` file per test. Worth adding a
   post-test snapshot if a bypass is ever suspected but not reproduced.

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
`grep -rn "noqa:.*Phase\|type: ignore.*Phase" --include="*.py" .` — treat an
empty result as "nothing deferred", not as a hidden backlog. (A bare
`grep -rn "Phase [0-9]"` is no longer a useful proxy for this check: Phase 3b
left dozens of comments and docstrings narrating what it changed and why,
none of which are deferral markers.)
