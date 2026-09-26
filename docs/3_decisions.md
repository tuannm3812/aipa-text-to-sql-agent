# Decision Log

Newest first. Each entry states what was chosen and what it ruled out.

## 2026-09-26 — PostgreSQL fails closed on an over-privileged role

**Chosen:** `PostgresEngine.check_reachable()` now also queries `pg_roles`
for `current_user` and raises the new `EngineForbiddenError` (`engines/
base.py`) if the connecting role is a superuser or holds
`pg_read_server_files`, `pg_write_server_files` or
`pg_execute_server_program` - naming which condition held and pointing at
`docker/postgres-init.sql`'s `aipa_ro` role as what a correctly provisioned
one looks like. `EngineForbiddenError` is a new `EngineError` subclass,
deliberately **not** also a `FileNotFoundError` the way `EngineUnreachable
Error` is - "reachable but refused" is not "not found." `check_reachable()`
re-raises it ahead of the broad `except Exception -> EngineUnreachableError`
wrapping so it is not misreported as unreachable, and it propagates through
`pipeline.ask_database`/`ask_database_with_sql` (called before their own
`try`, the same place `EngineUnreachableError` already propagated from) to
`ui/chat.py`'s `_run_query` and `ui/sidebar.py`'s `active_db_path`, both of
which already catch bare `Exception` and redact before display - no new
catch site was needed.

**Ruled out:** Trusting every deployment to provision its role like `docker/
postgres-init.sql` and documenting the requirement instead of checking it;
reusing `EngineUnreachableError` for this failure; checking on every
`execute()` call instead of once at `check_reachable()`.

**Why:** DuckDB's defence against reading host files
(`enable_external_access=False`) is enforced by the engine itself, whatever
file is opened. PostgreSQL has no equivalent connection flag - its defence
is entirely a property of how the connecting role was provisioned, which
this codebase does not control. A superuser DSN (or a role a deployment
over-granted for unrelated tooling) pasted into the "Connection string"
sidebar field would otherwise connect successfully with no filesystem
protection at all and no error naming why - exactly the class of hole
`test_engine_postgres.py`'s Task 3 probes proved `aipa_ro` itself is refused.
Reusing `EngineUnreachableError` was ruled out because it also inherits
`FileNotFoundError` for a documented legacy-caller contract that has nothing
to do with this failure; claiming it here would misdescribe "reachable but
refused" as "not found" to any caller that branches on that type
specifically. Checking once at `check_reachable()` rather than per-query is
correct because the role does not change between queries on one DSN -
`docker/postgres-init.sql`'s superuser fixtures (`_as_postgres_superuser` in
several test files) stay outside the engine on purpose, connecting directly
via `psycopg.connect` to set up schemas or prove the read-only transaction
flag is load-bearing without the role, never through `PostgresEngine.
check_reachable()`, so this check was not weakened to keep them working - the
new tests (`test_check_reachable_refuses_a_superuser_dsn` et al.) call
`check_reachable()` directly against a superuser DSN to prove the check is
itself honest.

## 2026-09-26 — schema scope is opt-in, via `AIPA_EXTRA_SCHEMAS`

**Chosen:** `PostgresEngine`/`DuckDBEngine` read only their own default
schema (`public`/`main`) plus whatever schema names
`engines/base.py::extra_schemas_from_env()` finds in the `AIPA_EXTRA_SCHEMAS`
environment variable (comma-separated, whitespace-trimmed), read once per
engine instance in `__init__`. PostgreSQL still additionally requires
`has_schema_privilege` on each opted-in name - naming a schema does not by
itself grant access to it. Both engines' `schema_fingerprint()` now include
the opted-in set (sorted) directly, not only its visible effect, so
`schema.py`'s cache invalidates on a config change even when that change
happens to produce the same effective table list today.

**Ruled out:** A constructor argument threaded through `open_engine(dsn)`
and every caller (`ui/settings.py`'s `Settings`, the sidebar) - opt-in scope
is deployment-level configuration set once, not a per-question choice, and
`open_engine` takes only a DSN today; a DSN-level query parameter - a DSN is
already handled everywhere in this codebase (`dsn.py`, `ui/uploads.py`) as a
single opaque, credential-bearing string, and `redact_dsn`/the sidebar's
"Using `<dsn>`" caption would all need to start parsing it apart again;
leaving Task 6's "every schema the role can read" as it was and documenting
the risk instead of closing it.

**Why:** Phase 3b Task 6 widened DuckDB and PostgreSQL from their own
default schema to every schema the connecting role could read, so that
schema's table names, columns and DDL are sent to the LLM provider. A
deployment may have granted its read-only role `USAGE` on a staging or PII
schema for some unrelated tool; that must not silently become LLM-visible
just because this agent widened its own catalogue read. An environment
variable matches how this codebase already handles single, deployment-level
settings read directly from `os.environ` (`ui/secrets.py`'s
`active_gemini_key` reads `GEMINI_API_KEY` the same way) without adding new
plumbing through `Settings`/the sidebar for something with no reason to vary
per question. Both engines had to change together, since DuckDB has no
privilege model to fall back on - `AIPA_EXTRA_SCHEMAS` is the *only* gate for
DuckDB, where for PostgreSQL it is one of two (opted in **and** privileged).
`raw_schema()`, `schema_chunks()`, `table_names()`, `table_columns()` and
`schema_fingerprint()` all resolve the opted-in set the same way per engine
(`PostgresEngine._user_schema_names`, `DuckDBEngine._allowed_schemas`), so a
table the model is shown and a table the validator accepts stay the same set
- the "advertised then blocked" inversion this phase already closed twice
(`docs/3_decisions.md`'s 2026-09-25 and 2026-09-26 schema-identity entries)
does not reopen a third time from a scope mismatch between the opt-in and
the validator.

## 2026-09-26 — schema-qualified table identity, carried through every engine

**Supersedes** the 2026-09-25 entry below, which was explicitly a placeholder
for this work.

**Partly superseded, same day, by "schema scope is opt-in, via
`AIPA_EXTRA_SCHEMAS`" above.** What this entry decided — one schema-qualified
identity, produced by the engine and consumed unchanged by every layer — still
holds in full and is what ships. Only the *size of the universe* it is applied
to changed: the two claims marked below described a scope that was widened
here and then narrowed by owner decision before the phase closed. Neither
engine reads every schema it can query; both read their own default schema
plus whatever `AIPA_EXTRA_SCHEMAS` opts into.

**Chosen:** One identity, produced by the engine and consumed unchanged by
every layer. `SchemaChunk` gains `schema_name`, set only when the table is
outside the engine's `default_schema`, and `qualified_name` renders it
(`analytics.thing` outside, bare `customers` inside). ~~DuckDB and PostgreSQL
read every schema they can actually query~~ — **superseded, see above:** both
read their default schema plus the `AIPA_EXTRA_SCHEMAS` opt-in, PostgreSQL
through `_user_schema_names`, which additionally asks `has_schema_privilege`
on each opted-in name, and neither reads `pg_catalog`, `information_schema` or
any other `pg_*`. `Engine.table_names()`
returns two spellings for a default-schema table (bare *and* qualified) and
exactly one for every other (qualified only), via the single shared
implementation `engines/base.py::table_name_spellings`. `safety.py`'s
hardcoded `schema == "main"` rule is gone: a qualified reference is checked in
its qualified form against that same set.

**Ruled out — accepting a bare name for a table outside the default schema.**
This is the ambiguity question `search_path` answers on a real server, and
answering it any other way reintroduces one of the two 2026-09-25 failures.
Accepting `thing` for `analytics.thing` would approve a query the engine then
fails to resolve (`CatalogException`), which is the "advertised then blocked"
inversion in mirror image; and where two schemas share a name, it would
validate a column list belonging to whichever table the guess picked. A bare
name therefore means the default schema's table or nothing — exactly what the
engine itself does — so it is never ambiguous, and `main.shared` and
`analytics.shared` coexist as two distinct entries.

**Ruled out — a second schema for SQLite.** SQLite's only route to one is
`ATTACH DATABASE`, which this project has no shape for: a SQLite DSN is a
single file path, and the read-only authorizer denies `SQLITE_ATTACH`
outright. SQLite therefore keeps its catalogue reads exactly as they were and
only learns the `main.<table>` spelling it already accepts. Parity here would
have been fiction; `tests/test_schema_identity.py::
test_sqlite_has_exactly_one_schema_because_attach_is_denied` pins the real
shape instead.

**Why:** The 2026-09-25 decision closed the inversion by narrowing all three
layers to agree on `main`, at a stated cost — "a DuckDB database whose tables
all live outside `main` now presents an empty schema and answers
`UNANSWERABLE_WITH_GIVEN_SCHEMA`". PostgreSQL makes that cost untenable:
`public` is only a default and multi-schema databases are ordinary. The layers
still have to agree, so the agreement moved from "show less" to "one spelling,
produced once": what `raw_schema()` shows the model is what `table_names()`
accepts is what the engine runs. The two 2026-09-25 failure reproductions were
kept and inverted rather than deleted
(`tests/test_engine_duckdb.py`'s two cross-schema tests), because a future
refactor of value-hint or chunk-keying code must still hit the
`BinderException` they were written for.

**Cost:** the table and column universes the default-deny validator checks
against are now larger — ~~every schema the role may use, not one~~
(**superseded, see above:** the default schema plus the `AIPA_EXTRA_SCHEMAS`
opt-in, so on a deployment that sets nothing the universe is unchanged at one
schema). That is the same set the engine would execute against, so it is not a
widening relative to reality, and the internals rules (`pg_catalog.*`, `information_schema.*`,
`sqlite_*`) are name rules that consult no table list and are unaffected.
`tests/test_schema_identity.py::test_the_closed_bypasses_stay_closed_with_a_
second_schema` re-runs this phase's closed bypasses against a database that
has a second real schema in it.

## 2026-09-26 — PostgreSQL's read-only guarantee is two mechanisms, each proven load-bearing alone

**Chosen:** Two independent mechanisms, neither trusted alone: a least-privilege
`aipa_ro` role (`docker/postgres-init.sql`, no `INSERT`/`UPDATE`/`DELETE`/DDL
grants) and `conn.read_only = True`, set before the first statement on every
connection `PostgresEngine._connect_read_only` opens — the read-only
transaction PostgreSQL itself enforces at parse/rewrite time.

**Ruled out:** The role alone — assumes every deployment provisions `aipa_ro`
correctly, and a superuser DSN (or any over-privileged role) pasted into the
sidebar's "Connection string" field would otherwise connect with zero
filesystem/write protection. The transaction flag alone — a role holding
`pg_read_server_files`/`pg_write_server_files`/`pg_execute_server_program`
would still reach those functions from inside a read-only transaction, since
`read_only` stops writes, not catalogue/file-surface calls. A shared
read-only mechanism factored out across engines — ruled out already by the
2026-09-19 entry below; PostgreSQL's pair adds a second, unrelated mechanism
to that same "nothing to factor out" conclusion.

**Why:** Proven independently load-bearing, not merely both present, against
the live container. Connecting as `aipa_ro` with `read_only` left unset and
attempting `INSERT INTO customers VALUES (999, 'Mallory')` raised
`psycopg.errors.InsufficientPrivilege` — the role's own grants are the only
thing that stopped it
(`test_the_role_is_load_bearing_without_the_read_only_transaction`).
Connecting as the `postgres` superuser with `read_only = True` and attempting
the identical `INSERT` raised `psycopg.errors.ReadOnlySqlTransaction` instead —
the transaction flag is what stopped it in that case, with no role privilege
involved
(`test_the_transaction_flag_is_load_bearing_for_a_privileged_connection`).
Removing either mechanism opens exactly the hole the other was silently
covering. This is the same "neither defence alone is sufficient" shape
`docs/0_coding_standards.md` §4 states for `is_safe_query` plus the read-only
authorizer, applied to PostgreSQL's own pair of mechanisms.

## 2026-09-26 — default-deny validation for PostgreSQL, and the four leaks that shaped it

**Chosen:** `PostgresEngine.allowed_functions` switched from `None`
(blocklist-only) to a 74-name default-deny allowlist — the same mechanism
`safety._references_disallowed_function`/`_references_unknown_table` already
enforce for DuckDB — because PostgreSQL registers 3,286 functions across
`pg_catalog` and `public` (confirmed live against `aipa_ro`'s own catalogue
view), too large to enumerate as a blocklist, the same conclusion Phase 3a
reached for DuckDB at 945.

**Ruled out:** A per-name blocklist of known-dangerous functions
(`pg_read_file`, `dblink`, `lo_import`, ...) — an allowlist only has to be
complete once; a blocklist has to stay complete forever, against a catalogue
this large and against every future PostgreSQL extension a deployment might
install.

**Why, and the four leaks that shaped it:** A name-based default-deny walk
restricted to `exp.Func` AST nodes turned out to have four independent ways
past it, each reproduced live before being closed, and each closed by moving
from "is this node a disallowed function call" to "does this identifier
*resolve* to a real, permitted name" — a resolution rule, not a name rule:

1. **`(expr).name` field notation.** PostgreSQL grammar sugar for `name(expr)`
   when `expr`'s type has no field called `name`; sqlglot parses the bare form
   as `exp.Dot`, never `exp.Func`, invisible to the function walk. Reproduced
   live: `SELECT ('/etc/passwd').pg_read_file` validated as safe. Closed by
   `_references_disallowed_dot_call`, gated to `_DOT_CALL_DIALECTS =
   {"postgres"}` because DuckDB parses the *identical* `exp.Dot` shape for its
   own legitimate struct/map field access (`(struct_col).field_name`), which
   is real user data, never a function-call reading — proven inert for DuckDB
   with a real `STRUCT`-typed column.
2. **`::regclass`-family OID casts.** PostgreSQL's eleven `reg*` object-identifier
   types (`regclass`, `regrole`, `regproc`, ...) resolve a string/OID directly
   against `pg_class`/`pg_authid`/`pg_proc`/... with no function call and no
   table reference for either existing check to see. Reproduced live:
   `SELECT ('customers'::regclass).pg_relation_filepath` returned a real
   on-disk path; a `generate_series` scan cast to `regclass` enumerated 422
   relation names, `regrole` enumerated 16 role names. Closed by
   `_casts_to_object_identifier_type`, keyed on sqlglot's own
   `exp.ObjectIdentifier` cast-target node — a shape only these eleven types
   ever produce (confirmed inert for DuckDB, which has no `reg*` types and
   parses the identical cast as plain `exp.DataType`).
3. **`alias.name` column-call sugar.** PostgreSQL's function-call syntax needs
   no parentheses: `alias.name`, where `name` is not a real column of
   `alias`, resolves as `name(alias)`. sqlglot parses this as `exp.Column`
   — not `exp.Func`, not `exp.Dot`, not `exp.Table` — invisible to every rule
   above, *including* the `pg_` internals rule. Reproduced live: a query
   `is_safe_query` approved ran `pg_terminate_backend` against a real backend
   PID. Closed in two parts: `_references_internal_column_name` extends the
   existing `internal_prefixes`/`internal_names` rule to a column's own name;
   `_references_unresolvable_qualified_column` (backed by a new
   `Engine.column_names()`) requires a *qualified* column name to be a real
   column of an advertised table or a name the statement itself binds (an
   `AS` alias, a `TableAlias` column list, a table-valued function's default
   output column).
4. **The `column_names()` whole-database union that briefly re-armed leak 3.**
   The first shipped version of `column_names()` unioned every schema the
   connection could read, so any column named after a single-argument
   catalogue function anywhere in that union — e.g. a hostile
   `ext.audit(lo_get)` sitting in an opted-in schema — re-validated
   `g.lo_get` regardless of which table `g` actually was, a regression proven
   `False` before that change and `True` after it. Closed by replacing the
   flat union with `table_columns()`, scoped to the tables the *statement
   itself* references, so a function-scan alias contributes no real table's
   columns.

**The lesson, carried forward:** a resolution rule is only as safe as what it
resolves *against*. Leak 4 is what happens when a resolution rule is built
against everything a connection can see rather than everything the query's
own structure actually binds — the same distinction that makes leak 3's fix
correct and would, if skipped, make it just as bypassable as the name rule it
replaced. Every subsequent qualified-column check in this codebase resolves
against the statement's own referenced tables, never the full catalogue.

## 2026-09-26 — the structural exemption for pure-syntax nodes (`AND`/`OR`/`EXISTS`)

**Chosen:** `_PURE_SYNTAX_FUNC_TYPES = (exp.And, exp.Or, exp.Exists)` in
`safety.py` — an `isinstance` skip inside `_references_disallowed_function`'s
`exp.Func` walk, exempting these three specific AST node *classes* from the
default-deny check, not by adding their resolved names (`"and"`, `"or"`,
`"exists"`) to either engine's allowlist.

**Ruled out:** Allowlisting `"and"`/`"or"`/`"exists"` as names. Considered and
rejected because it would change what the allowlist means to a reader — today
every entry in `PostgresEngine.allowed_functions`/`DuckDBEngine
.allowed_functions` names a real catalogue dispatch; widening it to also carry
pure syntax would blur that meaning for no safety benefit, since neither
keyword can ever reach a catalogue function regardless of whether its name
sits in the set.

**Why:** sqlglot 27 (27.29.0) models the reserved infix keywords `AND`/`OR`
and the `EXISTS (...)` predicate as `exp.Func` subclasses (`exp.And`,
`exp.Or`, `exp.Exists`) as an implementation detail of its own AST, not
because either engine treats them as catalogue functions. Because
`_references_disallowed_function` walked every `exp.Func` node and rejected
any whose resolved name was unlisted, any query with two conditions joined by
`AND`/`OR`, or containing an `EXISTS` subquery anywhere, was refused with
`BLOCKED_UNSAFE_SQL` — most real analytical SQL, live on DuckDB since Phase 3a
and missed by both engines' corpora (58 DuckDB queries, 32 PostgreSQL queries
at the time) because neither happened to combine two `WHERE` conditions with
`AND`. Verified live that neither keyword has any spelling under which either
dialect's grammar treats it as a function call — `and(a, b)` is a
`ParseError` under both — so there is no catalogue entry either could ever
dispatch to, and exempting the node type introduces no bypass. The exemption
is node-scoped, not subtree-scoped: `find_all` still walks every descendant,
so a disallowed call nested inside an `AND`/`OR`/`EXISTS` is still rejected
(verified: `WHERE customer_id = 1 AND current_setting('x') = 'y'` stays
rejected). `exp.Xor` was deliberately left *outside* the exemption as a
control: under PostgreSQL, `xor(true, false)` is real parenthesised call
syntax, not an infix keyword, and does reach the catalogue, so it stays
subject to the ordinary allow/deny check like any other function name — proof
the exemption is keyed to what the grammar can produce, not to "every
boolean-sounding node."

## 2026-09-26 — the catalogue-hash fingerprint, and its cost

**Chosen:** `PostgresEngine.schema_fingerprint()` hashes a deterministic
catalogue read — `sha256(repr(sorted rows))` over `information_schema.columns`
plus a `pg_constraint`-derived foreign-key row set — rather than a filesystem
`mtime`, because there is no single file to `stat` for a server-backed
engine.

**Ruled out:** Hashing only column rows, with no foreign-key row set (the
first shipped version) — rejected because a foreign key added or dropped with
no accompanying column-level change would leave the fingerprint, and
therefore `schema.py`'s `lru_cache` key, unchanged while `raw_schema()`'s
synthesised DDL and `schema_chunks()`'s neighbour graph both actually
changed. A server-side "last DDL timestamp" — PostgreSQL exposes no built-in
equivalent to the mtime SQLite/DuckDB get for free from their own files.

**Why, and the cost:** `schema.py::get_schema_chunks` calls
`open_engine(dsn).schema_fingerprint()` on *every* call — cache hit or miss —
because the fingerprint is itself half of the `lru_cache` key, so the engine
must be asked before the cache can even be consulted. For PostgreSQL that
means **one live catalogue query against the server per schema-chunk cache
check**, not only on a genuine miss — a cost SQLite/DuckDB do not pay in the
same way, since their fingerprint is a local filesystem `stat`. Accepted
rather than engineered around: the query reads catalogue metadata only (no
row data), runs once per question rather than once per retrieved chunk, and
the only honest alternative — trusting an external signal that the catalogue
changed — has no source a networked PostgreSQL server exposes. The
fingerprint also folds in `default_schema` and the sorted `AIPA_EXTRA_SCHEMAS`
opt-in set (see the schema-scope entry above) so a `search_path`/scope change
invalidates the cache even when the effective table list happens to be
unchanged.

## 2026-09-26 — per-engine `default_work_limit`, replacing one SQLite-shaped constant

**Chosen:** `Engine.default_work_limit: int` added to the protocol.
`SQLiteEngine.default_work_limit = DEFAULT_MAX_VM_STEPS` (100,000 VM steps,
unchanged). `DuckDBEngine.default_work_limit` and `PostgresEngine
.default_work_limit` both set to a new `DEFAULT_WORK_LIMIT_MS = 5_000`
(milliseconds). `execution.execute_query`'s `max_vm_steps` parameter default
changed from `DEFAULT_MAX_VM_STEPS` to `None`, reading `engine
.default_work_limit` when unset; an explicit value (including `0`, which
still disables the guard) still passes straight through.

**Ruled out:** Leaving `DEFAULT_MAX_VM_STEPS` as the one shared default and
converting units at each call site; a single cross-engine constant with no
per-engine override.

**Why:** `execution.execute_query` passed `DEFAULT_MAX_VM_STEPS` (100,000, a
SQLite VM-*instruction* count) verbatim as `work_limit` regardless of which
engine `open_engine` resolved, so DuckDB's `threading.Timer(work_limit /
1000, ...)` and PostgreSQL's `SET LOCAL statement_timeout = work_limit` both
received a **~100-second** timeout instead of the design's intended 5 seconds
(`QUERY_ABORTED_AFTER_5000_MS`, per `docs/superpowers/specs/
2026-09-14-phase-3-engine-abstraction-design.md` §4.5). A call-site audit
(`pipeline.py`'s four `execute_query` calls, `evaluation.py`'s one,
`tests/test_execution.py`'s explicit `max_vm_steps=0` override) found no
caller that ever passed a value expecting SQLite's unit specifically, so the
fix is a pure default-value change touching zero call sites. Verified live
and pinned: `SELECT pg_sleep(20)` with no explicit `max_vm_steps` now returns
`QUERY_ABORTED_AFTER_5000_MS` in well under 15 seconds
(`test_a_slow_postgres_query_aborts_near_5_seconds_not_100`, confirmed
passing against the live container in ~5.5s of wall time including pytest's
own overhead); SQLite's own `QUERY_ABORTED_AFTER_100000_VM_STEPS` behaviour,
and its pinning test, are unchanged.

## 2026-09-26 — `AIPA_TEST_POSTGRES_DSN`-or-skip locally, CI asserting no skip

**Chosen:** `tests/conftest.py`'s `postgres_dsn` fixture reads
`AIPA_TEST_POSTGRES_DSN`; when unset, or the driver/server is unreachable, it
skips every test that depends on it (`pytest.importorskip`/`pytest.skip`)
rather than failing. `.github/workflows/tests.yml`'s `test` job runs a
`postgres:16` GitHub Actions service container, applies
`docker/postgres-init.sql` (via `psql`, since a service container takes no
volumes to mount it directly) against the container's superuser bootstrap
account, sets `AIPA_TEST_POSTGRES_DSN` to the same least-privilege `aipa_ro`
DSN a real deployment would use, then re-runs `pytest -m conformance -rs` and
greps its output for a `^SKIPPED` line, failing the build if one is found.

**Ruled out:** Hard-failing PostgreSQL tests locally when Docker isn't
running — would break the inner loop of every contributor who hasn't started
`docker/postgres.yml`. Trusting `pytest -m conformance`'s own exit code as
the CI gate — it exits `0` on an all-skipped run, since "0 tests failed" is
true whether or not any test actually ran.

**Why:** The same failure mode this project already guards against for
`duckdb` (`uv sync --extra engines` plus a CI step asserting no conformance
test skipped — 2026-09-19 entry below) applies identically to a driver that
needs a *running server* rather than an installed package: silently skipping
every PostgreSQL conformance test would let the `test` job go green having
tested nothing new. Running the suite as `aipa_ro` in CI, not the container's
superuser bootstrap account, is deliberate rather than incidental:
`PostgresEngine.check_reachable()` now refuses a superuser DSN
(`EngineForbiddenError`, see above), so testing through the superuser account
would never exercise the real deployment path and would hide exactly the
class of gap Task 3's file/program-surface probe work was built to close.
Verified: with the DSN set, `pytest -m conformance -rs` reports `36 passed,
0 skipped` (12 per engine × SQLite/DuckDB/PostgreSQL); with it unset, the
same command reports the PostgreSQL third skipped with a message naming the
exact env var and the exact compose command to fix it, and CI's grep step
would fail the build in that state.

## 2026-09-25 — DuckDB support is scoped to the `main` schema, consistently across every layer

**Superseded by the 2026-09-26 entry above.** It was always a placeholder for
Phase 3b Task 6; the two failure modes it records are still the ones to avoid,
and are still pinned by the same two tests.

**Chosen:** Every DuckDB catalogue query — `raw_schema()`, `schema_chunks()`,
and the value-hint query beneath them — filters to `schema_name = 'main'`
(`table_schema` for `information_schema.columns`), and the value-hint query
is schema-qualified so it cannot resolve to another schema's same-named
table. `safety.py`'s existing "absent or `main`" rule is left untouched and
is now the layer the others agree with.

**Ruled out:** Carrying schema-qualified table identity through DDL, chunks,
value hints, foreign keys, `table_names()` and safety validation. Deferred to
Phase 3b.

**Why:** Codex's 2026-09-21 review (logged in `docs/6_agent_log.md`) found
the three layers disagreeing about the supported surface, and both failure
modes reproduced. A duplicate table name across schemas crashed schema
building with a `BinderException` before any question reached the model,
because the value-hint query asked unqualified `shared` — resolving to
`main.shared` — for a column only `analytics.shared` has. A table living
only outside `main` was advertised to the model by `raw_schema()` and then
rejected by the validator when the model correctly qualified it, while the
unqualified form passed validation and failed at execution. Every route
failed, and the user saw `BLOCKED_UNSAFE_SQL` or a repair loop.

Filtering consistently removes the inversion at the source: a table outside
`main` is never advertised, so the model is never shown something it is not
allowed to query. Full schema-qualified identity is the better end state,
but PostgreSQL forces the same question for both engines in Phase 3b — where
schemas are unavoidable, `public` being the default and multi-schema
databases normal — so doing it once there beats doing it twice. The cost of
this decision is explicit: a DuckDB database whose tables all live outside
`main` now presents an empty schema and answers
`UNANSWERABLE_WITH_GIVEN_SCHEMA`. That is a documented limit, not a bug, until
3b lifts it.

## 2026-09-25 — dialect-dependent prompt instructions are engine-owned, parameterised rather than reworded

**Chosen:** `_PROMPT_BODY` in `llm.py` carries three placeholders —
`{{DIALECT_SECTION}}`, `{{DIALECT_NAME}}` and `{{ENGINE_RULES_BLOCK}}` —
filled from engine attributes (`prompt_dialect_section`,
`prompt_dialect_name`, `prompt_engine_rules_block`). `_repair_sql` in
`pipeline.py` interpolates the same dialect name into the repair *user*
prompt.

**Ruled out:** Rewording the shared body into dialect-neutral prose (e.g.
"a single read-only SELECT query", "SQL `=` is case-sensitive").

**Why:** Phase 3a split the prompt but left four SQLite instructions in the
supposedly shared half, so every DuckDB generation was told to write "a
SINGLE SQLite SELECT query" and to avoid `sqlite_master`, and every DuckDB
repair reported a "SQLite error" and asked for a "corrected SQLite SELECT
query" — found by Codex on 2026-09-21 and reproduced. Neutral rewording is
the cleaner long-term shape, but it necessarily changes the bytes of the
shared body and therefore of the assembled SQLite prompt, whose sha256
(`89d91c…`) is pinned precisely so this refactor cannot alter SQLite's
behaviour. Per-engine substitution reproduces SQLite's wording byte for byte
while naming DuckDB correctly. The case-insensitivity rule is engine-owned
for the same reason even though the underlying claim holds for both engines:
its attribution, not its content, was wrong. If the sha256 pin is ever
deliberately retired, collapsing these back to neutral wording is the
preferred shape.

## 2026-09-19 — per-engine read-only enforcement, no shared mechanism

**Chosen:** Each `Engine` implementation proves its own read-only guarantee
its own way — SQLite via a `PRAGMA query_only` connection plus a 28-constant
authorizer, DuckDB via `read_only=True` plus `enable_external_access=False`
on connect. `tests/test_engine_conformance.py`, parametrised over every
available engine with no per-engine special-casing in the test bodies, is
the thing that proves the guarantee actually holds for each one.

**Ruled out:** A shared read-only enforcement layer in `Engine`/`base.py`.

**Why:** SQLite's URI flag plus authorizer, DuckDB's connect-time flags and
(from Phase 3b) PostgreSQL's read-only transaction share nothing but the
outcome — there is no common mechanism to factor out without inventing an
abstraction none of the three drivers actually has. Making the conformance
suite the shared proof, rather than the implementation, is what lets each
engine use its native mechanism while still being held to the same
guarantee: `tests/test_engine_conformance.py::test_writes_are_refused_by_the_connection_itself`
and its siblings run unmodified against `[sqlite]` and `[duckdb]` alike.

## 2026-09-19 — `enable_external_access=False` is DuckDB's load-bearing filesystem guard, not `read_only=True`

**Chosen:** `DuckDBEngine`'s connection is opened with both
`read_only=True` and `config={"enable_external_access": False}`.

**Ruled out:** `read_only=True` alone, on the assumption that "read-only"
already implies "can't touch the filesystem."

**Why:** `read_only=True` stops writes to the database file; it does nothing
about DuckDB reading arbitrary files through the query language itself.
Verified directly (Task 6): with `enable_external_access=False` removed and
only `read_only=True` kept, `read_csv('<path>')`, `SELECT * FROM '<path>'`
(both quoted and unquoted, via DuckDB's replacement scan), `glob('<path>')`,
and `COPY ... TO '<path>'` all succeeded against files outside the target
database — every one of the 4 filesystem-access probes in
`tests/test_engine_duckdb.py` failed (`DID NOT RAISE Exception`) with the
setting removed and passed with it restored. `enable_external_access=False`
is therefore the actual defence; `is_safe_query`'s DuckDB internals list and
default-deny function/table checks are defence-in-depth on top of it, not a
substitute for it — the bare quoted-path form (`FROM '<path>'`) has no
function name for an AST check to catch at all, which is exactly why the
connection-level setting has to hold on its own.

## 2026-09-19 — default-deny validation for DuckDB, chosen after the name blocklist leaked four times

**Chosen:** For any engine whose `allowed_functions` is not `None`
(currently only `DuckDBEngine`), `safety.is_safe_query` switches from a
blocklist (name a few internal things, reject them) to default-deny (name
everything that's allowed, reject anything else). Concretely, four gates:

- **A function allowlist checked in every position** — scalar, aggregate,
  window, and table — not just the table-source position the old blocklist
  checked. `_resolve_function_name` maps a parsed call back to the real
  DuckDB name it invokes (sqlglot's own `.sql_name()` sometimes diverges,
  e.g. `date_trunc(...)` parses to `TimestampTrunc` whose `.sql_name()` is
  `TIMESTAMP_TRUNC`), and the result must be one of 127 allowlisted names.
- **A table default-deny** — every `FROM`/`JOIN` target must name either a
  real table (`Engine.table_names()`) or a CTE visible at that point in the
  statement, or the query is rejected. This is what closes the replacement-scan
  path a bare `FROM '<path>'` or `FROM path` (no quotes) takes, which no
  function-name check can see.
- **`list_aggregate`'s (and its four synonyms') string-dispatch argument**
  must be a literal naming an allowed function — `list_aggregate(col,
  'histogram')` genuinely runs `histogram` at the SQL level even though
  `histogram` is itself rejected everywhere else, so the dispatch argument
  needs its own check.
- **Scope-correct CTE visibility** — a CTE name is only "real" where DuckDB
  itself would resolve it: visible in the query that owns its `WITH` clause
  and nested subqueries of that query, not in a sibling or outer query, and
  (within one `WITH` list) only to CTEs defined after it, unless the list is
  `RECURSIVE`. A forward reference or an outer-sibling reference to a CTE
  name falls through to a real replacement scan in DuckDB, so the validator's
  visibility rule has to match that exactly rather than treating every CTE
  alias in the parsed tree as globally visible.

**Ruled out:** Continuing to patch the blocklist (`internal_prefixes`/
`internal_names`) as new gaps were found.

**Why:** Four rounds of blocklist fixes in Task 6/6b each closed the exact
case that prompted them and opened a new one: `information_schema.tables`
(schema-qualifier gap) and `read_csv`/`glob` (checked `exp.Anonymous` only,
missed sqlglot's typed `exp.Func` subclasses) in Task 6's first pass;
`sniff_csv`/`query`/`query_table` (a function-name sweep found more DuckDB
table functions than the hand-picked list covered) and administrative
functions like `checkpoint`/`enable_logging` executing despite passing
validation (the allowlist bar was "doesn't touch the filesystem," not least
privilege) in Task 6's post-review fixes; a comma-join and an unquoted
filename escaping the table-source string check, plus `EXTRACT(... FROM
...)` being falsely rejected by that same check, in Task 6b's review round.
Each fix was correct for the case in front of it and wrong one level out —
the same pattern already named in the 2026-09-14 agent log entry for the
SQLite safety fixes in Phase 2. Against DuckDB's much larger function
surface (945 distinct catalogue names swept in Task 6b, versus SQLite's
much smaller surface), a name blocklist is structurally unfit: it can only
be as complete as the last person to think of a name. Default-deny inverts
the burden — an unclassified future DuckDB function fails closed by
construction, pinned by
`tests/test_engine_duckdb.py::test_every_unlisted_duckdb_function_is_rejected_by_default_deny`,
which re-sweeps the live catalogue (945 names, 127 allowlisted, 0
mismatches across 1,836 scalar/table-position checks) rather than asserting
against a frozen list. `SQLiteEngine.allowed_functions` stays `None` — its
much smaller function surface and years of the existing blocklist holding
made the stronger guarantee not worth the added complexity there; the two
engines are allowed to make different calls for the same reason Phase 3's
governing constraint says the read-only model itself doesn't share a
mechanism.

## 2026-09-19 — abort-code family shares the `QUERY_ABORTED_AFTER_` prefix, diverges on unit

**Chosen:** `QUERY_ABORTED_AFTER_<n>_VM_STEPS` for SQLite (unchanged from
Phase 2), `QUERY_ABORTED_AFTER_<n>_MS` for DuckDB.

**Ruled out:** A single shared unit (e.g. forcing DuckDB's wall-clock budget
into a step count, or SQLite's step count into milliseconds).

**Why:** SQLite's abort mechanism is a VM-instruction-count progress
handler; DuckDB has no equivalent step counter exposed to a read-only
connection, so its budget is wall-clock. Sharing the `QUERY_ABORTED_AFTER_`
prefix keeps both codes recognisable as the same *family* of outcome (per
`docs/0_coding_standards.md` §3, every error code is
`SCREAMING_SNAKE_CASE` and the app/evaluation harness branch on the prefix
where they need to), while the differing suffix (`_VM_STEPS` vs. `_MS`)
keeps each engine honest about what it actually measured rather than
converting one unit into a fictitious equivalent of the other.
`ui/results.py`'s `describe_error` renders each with its own unit; a review
fix (commit `13b28ba`) closed a bug where the `_MS` suffix wasn't
recognised and rendered as "5000_MS database steps" verbatim.

## 2026-09-19 — schema fingerprint replaces a filesystem stat; cache keyed on `(dsn, fingerprint)`

**Chosen:** `Engine.schema_fingerprint() -> tuple[object, ...]` replaces the
old SQLite-specific `_db_cache_key` (which statted the file for `(mtime_ns,
size)`). `schema.py`'s `_cached_schema_chunks` is an `lru_cache` keyed on
`(dsn, fingerprint)`, with `fingerprint` computed by calling
`open_engine(db_path).schema_fingerprint()` before every lookup.

**Ruled out:** Keeping a filesystem-stat-only cache key, which has no
meaning for a DSN that isn't a plain file path.

**Why:** A filesystem `stat()` is meaningless for an engine whose target
isn't a bare file — DuckDB's own file still has `(mtime_ns, size)`, but a
future PostgreSQL DSN has no local file to stat at all. Each engine now
produces whatever fingerprint fits its own target (SQLite and DuckDB both
still use `(str(path), mtime_ns, size)` today, since both are file-backed);
the cache only needs the fingerprint to change whenever the schema does, not
to mean anything outside that engine. Verified for DuckDB specifically
(Task 6): a `CREATE TABLE` through a second writable connection changes the
file's `mtime_ns` even though DuckDB buffers writes, and `schema_fingerprint()`
picks that up, which is what
`tests/test_engine_conformance.py::test_the_fingerprint_changes_when_the_schema_changes[duckdb]`
pins.

## 2026-09-19 — `duckdb` is an optional extra, kept out of `requirements.txt`

**Chosen:** `duckdb>=1.0,<2` is declared under
`[project.optional-dependencies]` as both a `duckdb` extra and (bundled with
whatever else Phase 3b adds) an `engines` extra. `requirements.txt`, generated
by `uv export --no-hashes --no-dev --no-emit-project`, does not include it —
confirmed with `grep -c duckdb requirements.txt` returning `0`.

**Ruled out:** Making `duckdb` an unconditional dependency now that a second
engine exists.

**Why:** Per the phase's own global constraint, `requirements.txt` is what
Streamlit Community Cloud actually installs from for the hosted demo, and
the hosted demo only ever opens the bundled SQLite databases — a driver it
never loads would be dead weight on every cold start. CI installs the
`engines` extra explicitly (`uv sync --extra engines`) so DuckDB's tests
still run in the matrix; `open_engine`'s `duckdb://` branch wraps the import
in `try/except ImportError`, converting a missing driver into
`EngineUnavailableError` naming the extra to install, so a local install
without the extra fails with an actionable message instead of a bare
`ModuleNotFoundError`.

## 2026-09-19 — `EngineUnreachableError` inherits `FileNotFoundError`, raised before the pipeline's `try`

**Chosen:** `EngineUnreachableError(EngineError, FileNotFoundError)` in
`text_to_sql_agent/engines/base.py`. Both `ask_database` and
`ask_database_with_sql` call `engine = open_engine(db_path);
engine.check_reachable()` before entering their own `try` block, so an
unreachable DSN propagates as a raised exception rather than being caught
and folded into a returned `QueryResult.error`.

**Ruled out:** A new, unrelated exception type for unreachable engines;
catching the reachability check inside the existing `try` and returning it
as an error code like every other pipeline failure.

**Why:** Both pipeline entry points used to do `os.path.exists(db_path)`
directly and let a missing file surface as a plain `FileNotFoundError` to
any caller that checked for one specifically. Making the new engine-aware
check also satisfy `isinstance(.., FileNotFoundError)` keeps that documented
contract for existing callers without requiring them to learn a new type.
Raising before the `try` (rather than inside it, where every other pipeline
failure is caught and turned into `QueryResult.error`) is deliberate: a
database that cannot be reached at all is a caller error, not a query
outcome, and Task 7's `tests/test_end_to_end_engines.py` pins this with
`pytest.raises` rather than asserting on `result.error` — the two are not
interchangeable. Verified: `str(EngineUnreachableError("input database not
found"))` is `"input database not found"`, `.errno` is `None`, and it is
simultaneously `isinstance(.., FileNotFoundError)` and `isinstance(..,
EngineError)`.

## 2026-09-19 — the UI connection string is session-only; redaction happens at three points

**Chosen:** The new "Connection string" sidebar option stores its DSN only
in `st.session_state["sb_dsn"]` (a `type="password"` text input), read fresh
on every call to `ui/uploads.py`'s `active_db_path` and never written to
disk or to any cache key beyond the DSN string itself. `ui/uploads.py`'s
`redact_dsn` masks the credentials portion of any `scheme://user:password@`
substring, applied at three points: the sidebar's "Using `<dsn>`" caption,
the sidebar's ingestion-error handler (`st.sidebar.error`), and
`ui/results.py`'s `describe_error` for any unrecognised error code (since
the backend also puts raw exception text in that field, and a future
PostgreSQL driver error commonly echoes the DSN it failed to reach).

**Ruled out:** Never displaying the DSN at all, even redacted (the other
Database-source options all echo back a confirmation of what was selected,
so silence here would be inconsistent); persisting the DSN across sessions.

**Why:** A DSN may carry a password (the phase's own global constraint), and
this is the first UI path where one can be typed in directly. Session-only
storage means the password never reaches disk. Redacting at the point of
display, rather than trying to avoid ever displaying the DSN, is what lets
the sidebar keep the same "confirm what's active" pattern the other four
options already use. A review round (commit `13b28ba`) found `redact_dsn`
itself was incomplete — it required a non-empty username and stopped the
password at the first `/` or `@`, so `postgresql://:pw@host`,
`u:pa/ss@host`, and `u:p@ss@host` each leaked all or part of the password —
and fixed it to match greedily to the last `@` before whitespace with an
optional username, accepting over-masking as the safe failure mode. The same
round found `describe_error` was passing unrecognised error text to the page
unredacted, which is exactly the path a Phase 3b PostgreSQL driver error
would take.

## 2026-09-14 — share case *scoring*, not case *running*

**Chosen:** `text_to_sql_agent.evaluation.score_case` returns a `CaseScore`
(`executed`, `row_match`, `value_match`, `exact_match`), and both the Streamlit
tab and `scripts/evaluate_text_to_sql.py` score exclusively through it. A test
asserts neither harness compares rows itself.

**Ruled out:** the full contract the Phase 2 design §4.3 specified —
`EvaluationCase`, `CaseOutcome` and a shared `run_case`. Also ruled out: leaving
the two harnesses to score independently.

**Why:** Phase 2 extracted only the cell and row helpers, so the two harnesses
still computed their own verdicts — and they disagreed. The UI compared rows
with no error guard, so two failed queries each returning `[]` scored as a
perfect match; a Codex review reproduced this with a gold query that trips the
VM-step guard, where the UI reported `value_match=True` and the CLI reported
`False`. Because an aborted query now *returns* a typed result rather than
raising, that path reached scoring instead of blowing up, which is what exposed
the gap.

Sharing the verdict is what removes the divergence. Sharing the *running* of a
case would also merge two genuinely different jobs: the CLI owns retries,
provider backoff and CSV/Markdown output, while the UI owns Streamlit progress
and a dataframe. Merging them is a larger refactor with no correctness payoff
now that the verdict is common, so it is deliberately not done. The gold
benchmark still reports 12/12, unchanged — the fix removes credit for failures,
not for genuine passes.

## 2026-09-14 — the internals check applies to table sources only

**Chosen:** `_references_internals` flags a `sqlite_`/`pragma_`/`dbstat` function
name only when it sits in a table-source position — reached through `FROM`, a
`JOIN`, a derived table or a subquery.

**Ruled out:** applying the name rules to every function call, which is what the
first version did.

**Why:** the broad form rejected harmless scalars. `SELECT sqlite_version()` and
`SELECT sqlite_source_id()` read no internal table, executed fine under the
read-only connection, and were allowed before Phase 2 — so blocking them was a
new false rejection rather than a deliberate hardening. It was disclosed in a
commit body at the time but never justified, and a Codex review was right to ask
for it to be narrowed or documented. Every table-valued bypass stays blocked,
including inside joins, derived tables and scalar subqueries, and the `dbstat`
and quoted-`pragma_*` regression cases are retained.

## 2026-09-11 — relaxed mypy for `app.py`/`ui.*`/`scripts.*` rather than full strict

**Chosen:** Extend `mypy`'s `files` to cover `app.py`, `ui/` and `scripts/` (26
files total, up from 14), with an override that relaxes exactly two settings —
`disallow_any_generics` and `warn_return_any` — for those modules only.
`text_to_sql_agent` keeps `strict = true` unchanged.
**Ruled out:** Leaving the presentation layer unchecked, as it was through
Task 6; running it under the same full `strict = true` as the backend package.
**Why:** With `mypy` scoped to `files = ["text_to_sql_agent"]`, a rename in
`__init__.py`'s `__all__` could leave `backend.<old_name>` rotting in `app.py`
with ruff, mypy, and pytest all green, and the app raising `AttributeError` on
the user's first query — confirmed by appending a bogus attribute reference and
watching every gate pass. Full strict mode over the presentation layer was
tried and rejected: Streamlit's decorated API returns `Any` in places strict
mode cannot resolve without casts that document nothing, and its generic
containers are often unparameterised. The two relaxed settings were chosen by
toggling four candidates apart one at a time; `disallow_untyped_defs = false`
and `ignore_missing_imports = true` turned out to do nothing load-bearing once
`disallow_incomplete_defs` and the existing third-party override were accounted
for, so they were dropped rather than kept as unexplained slack. Attribute
access against the backend package — the thing that actually rots — is still
checked in every module.

## 2026-09-11 — `ui/` beside `app.py`, not inside `text_to_sql_agent/`, and not in the wheel

**Chosen:** Split `app.py`'s 759 lines (367-line `main()`) into a `ui/`
package — `settings.py`, `sidebar.py`, `chat.py`, `evaluation.py`, `styles.py`,
`constants.py`, `secrets.py`, `uploads.py` — sitting next to `app.py` at the
repo root, with `app.py` reduced to a 51-line entrypoint. `ui/` is deliberately
excluded from `[tool.hatch.build.targets.wheel] packages`.
**Ruled out:** Putting the new modules inside `text_to_sql_agent/`; shipping
`ui/` in the wheel alongside the backend package.
**Why:** `text_to_sql_agent/` is a library the notebook and the evaluation
script import independently of Streamlit; presentation code does not belong in
it, and packaging it would publish a Streamlit-dependent module to consumers
who may not have Streamlit installed. Within `ui/`, the sidebar returns a
frozen `Settings` dataclass instead of writing choices into
`st.session_state` under string keys for the body to read back — that coupling
was invisible in a single 759-line file and would not have survived a module
split. `st.session_state` is kept for what it is actually for: values that
must survive a rerun, such as chat history and the evaluation dataframe.

## 2026-09-11 — keep the SQLite VM-step guard, make its abort a typed result

**Chosen:** Keep `execution.py`'s `set_progress_handler` runaway-query guard
(100,000 VM steps by default) and give its abort a typed `QueryResult.error`
of `QUERY_ABORTED_AFTER_<n>_VM_STEPS`, identified by
`exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT` rather than the exception's
message text. Renamed `DEFAULT_SQLITE_PROGRESS_STEPS` to
`DEFAULT_MAX_VM_STEPS` and the `progress_steps` keyword to `max_vm_steps`.
**Ruled out:** Removing the guard as dead/accidental behaviour; leaving it as
a bare `OperationalError: interrupted` that reaches the caller undifferentiated
from a genuine SQL error.
**Why:** The guard is a deliberate runaway-query protection for the hosted
demo — it stops a 60k-row self-join while all 12 gold evaluation cases
complete well inside the budget, so it earns its keep. What was wrong was the
reporting: the bare `OperationalError` was stringified into `QueryResult.error`
as meaningless text and, worse, `ask_database` treated it as a generation
failure and spent an LLM repair call on SQL that was never wrong. Matching by
`sqlite_errorcode` rather than message text means a future SQLite wording
change cannot silently break the distinction between an abort and a real error
(such as a missing table, which also raises a plain `OperationalError`).

## 2026-09-11 — `sqlglot` unavailable fails closed, not open

**Chosen:** `_is_safe_ast` returns `False` (reject) when `sqlglot` cannot be
imported, rather than `True` (allow) as it did previously.
**Ruled out:** Keeping the fail-open fallback that let `is_safe_query` approve
every query, unchecked, whenever the optional dependency was missing.
**Why:** The fail-open path was survivable only because the keyword regex
still ran in that case and caught the worst cases by accident. Once the regex
was replaced by an AST-only multi-statement check, a missing `sqlglot` would
have meant no validation ran at all while `is_safe_query` still reported
`True`. Refusing to run is the correct failure mode for a safety check: an
error a user can see and report beats a silent bypass.

## 2026-09-11 — multi-statement guard replaces the keyword regex in `safety.py`

**Chosen:** Replace `_DANGEROUS_SQL_PATTERN` (a regex matching
`insert|update|delete|drop|alter|create|...` anywhere in the SQL text) with an
explicit statement-count check via `sqlglot.parse`, plus an AST table-name
check (`_references_internals`) in place of the old
`sqlite_master`/`sqlite_schema` text match.
**Ruled out:** Deleting the regex outright once the sqlglot AST check existed;
patching the regex to exempt string literals and `REPLACE(...)` instead of
removing it.
**Why:** The regex matched keywords anywhere in the statement, including
inside string literals, so it rejected legitimate read-only SQL — `REPLACE()`
is a standard SQLite string function, and any literal containing `update`,
`delete`, or `create` (e.g. `WHERE note = 'please update'`) was blocked with
no way for the user to tell it was a false alarm. It could not simply be
deleted, though: `sqlglot.parse_one` inspects only the first statement, so
`SELECT 1; DROP TABLE t` would pass the AST check on the strength of its
harmless prefix — the regex was the sole thing catching stacked statements,
by accident. The replacement counts parsed statements explicitly instead. The
old text-matching internals check independently had a bypass:
`dbstat('main')` is a table-valued function, not a bare table reference to
`sqlite_master`/`sqlite_schema`, so the text match missed it and it executed,
returning real schema metadata; the new AST check walks both `exp.Table` and
`exp.Anonymous` (function-call) nodes and closes that. Verified against a
parametrised corpus of 9 legitimate and 17 dangerous inputs, zero leaks and
zero false rejects.

## 2026-09-11 — mypy strict over `text_to_sql_agent/`

**Chosen:** `mypy --strict` on the package only; `app.py`, `scripts/` and
`tests/` excluded.
**Ruled out:** No type checker, matching every sibling project that follows
this standard; the master standard itself asks for hints without enforcing
them.
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
from its module docstring and the three function docstrings — about 170
duplicated lines — and the tests exercised *its* copy, leaving `pipeline.py`
with no coverage. A fix applied to `pipeline.py` would not have reached the code
under test.

## 2026-09-11 — `uv` with a generated `requirements.txt`

**Chosen:** `pyproject.toml` plus `uv.lock` as the source of truth;
`requirements.txt` generated by `uv export` and committed.
**Ruled out:** Dropping `requirements.txt` entirely; keeping it hand-maintained.
**Why:** Streamlit Community Cloud reads `requirements.txt`, so it must remain
a real file at the root. (`.devcontainer/devcontainer.json` read it too at the
time of this decision; commit `bdb1eca`, later in this same phase, moved it to
`uv sync` instead, so this is now the Streamlit Cloud reason alone.)
Generating it keeps one source of truth; `tests/test_packaging.py` and CI fail
on drift.

## 2026-09-11 — Shape B doc numbering

**Chosen:** Renumber `docs/` to Shape B.
**Ruled out:** Leaving the ad hoc `academic/` and `supporting/` split, which
master §2 permits for existing repos.
**Why:** §2's exemption covers "repos being substantially reworked anyway", which
the five-phase roadmap is. Doing it now costs one commit; doing it after Phases
2-5 would churn far more links.
