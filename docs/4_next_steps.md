# Next Steps

Phases 1, 2, 3a, and 3b are complete. Phases 4-5 are specced in
`docs/superpowers/specs/2026-09-10-refactor-roadmap.md`; this file is the
prioritised working view.

## Evaluation v2 — what is left (do first)

The contract, harness, gates and first routine results landed on 2026-10-09.
On 2026-10-10 Ollama thinking became an explicit setting, off by default, after
it was found to leave many answers empty (`docs/3_decisions.md`). Remaining, in
order:

1. **Full release runs, in progress.** `spider_dev` full and `bird_dev` full
   with evidence on and off, with thinking off, at
   `--work-limit 1000000000 --max-rows 100000`. Started 2026-10-10 from a
   clean pinned worktree on commit `7708295`. At the measured 3-7 s per case
   they need about 7-8 hours; the harness resumes after any interruption. Only
   then may the README carry Spider/BIRD headline tables. The earlier runs at
   the model's default thinking are history: the Spider dev full run is
   committed in `509998b`, and the BIRD run stopped at 1,289 of 1,534 cases
   and is not committed.
2. **Gate G8, the RAG on/off ablation.** `cases.csv` now records the
   provider's prompt and completion tokens per case, summed over generation,
   repairs, retries and resumed outages, so the ablation can report cost as
   well as accuracy. Ollama's prompt count stays the same when a prompt prefix
   is served from its cache. The regression gate refuses by design to compare
   runs that differ in a setting such as `use_rag`, so G8 needs a paired
   comparison for one deliberate difference. The thinking comparison was
   computed with the harness's own paired bootstrap from a one-off script;
   G8 should make that a committed tool and re-derive it.
3. **Gemini may starve the same way. Unverified.** The Gemini call also caps
   output at 512 tokens, and `gemini-2.5-flash` also thinks by default. Check
   with a key whether its thinking counts against that cap and leaves empty
   answers on harder questions. If it does, disable it the same way
   (`thinking_budget=0`) and record the setting in the run identity.
4. **Carried forward, not fixed:** a database changed *during* an uninterrupted
   session is not detected (the fingerprint is taken at start and resume), and
   SQLite `-wal`/`-shm` side files are not hashed; schema recall uses a second
   retrieval call rather than the one the pipeline made; the manifest's prompt
   hash is rebuilt through a private `llm` helper; "same hostname and dead pid"
   is not proof across pid namespaces sharing a hostname. The v1 script
   (`scripts/evaluate_text_to_sql.py`) and the May tables remain as history.

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
