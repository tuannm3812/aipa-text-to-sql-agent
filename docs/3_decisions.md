# Decision Log

Newest first. Each entry states what was chosen and what it ruled out.

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
