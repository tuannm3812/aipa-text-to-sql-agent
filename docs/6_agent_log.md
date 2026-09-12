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

**Verified:** `uv run pytest` 20 passed (18 pre-existing plus two packaging guards). `ruff check` and `ruff format --check`
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

## 2026-09-11 — Task 9: end-to-end reconciliation of the Phase 1 entry above

Master §13 warns that an unverified finding repeated across handoffs hardens
into fact. The "Verified" paragraph above was composed in Task 7, before
several of the things it asserts had actually been checked. This entry
re-ran the full gate and traced each claim back to primary evidence,
correcting per the append-only rule rather than editing that paragraph.

**Confirmed true, with evidence:**
- `uv run pytest` → `20 passed, 5 subtests passed` (default `-q` addopts;
  `-v` suppresses the subtests count in this pytest version — a formatting
  quirk, not a missing check). Collection breakdown: 2+5+2+2+3+3+2+1 = 20,
  of which `tests/test_packaging.py` contributes exactly 2, matching "18
  pre-existing plus two packaging guards." First genuinely verified in
  Task 2/4/5/8 per `docs/superpowers/sdd/progress.md`; reconfirmed today.
- `requirements.txt` installs into a clean virtualenv — true, but only for a
  Python in the project's supported range (`>=3.11,<3.14`, per
  `pyproject.toml`). A clean venv built from this machine's system
  `python3` (3.9.6) fails to resolve `altair==6.2.2` and others, which
  require Python >=3.10 — expected and out of scope, not a regression.
  Reconfirmed today with a `uv`-provisioned Python 3.11 venv (full install
  succeeded). Originally verified in Task 1.
- `app.py`'s twelve `backend.*` attributes all resolve — true, and further
  confirmed today that all twelve are also members of
  `text_to_sql_agent.__all__` (not just `hasattr`-visible). Originally
  verified in Task 4 Step 6.
- Notebook JSON well-formed, 25 cells, 11 with outputs — true. Originally
  verified in Task 4 Step 8, re-verified after the Step 10 `ruff format`
  pass with an identical result. Reconfirmed today.
- The packaging drift guard was confirmed to fail when `sqlglot` was
  removed from `requirements.txt`, then restored — true. Originally done
  in Task 5 (and re-verified at the CI level in Task 8). Reconfirmed today
  live: removing the `sqlglot` line fails
  `test_requirements_txt_covers_every_runtime_dependency` with the expected
  "stale, missing ['sqlglot']" message; restoring the file makes both
  packaging tests pass again, and `git status --short requirements.txt`
  showed no diff afterward.

**Corrected — asserted before it was checked:**
- "Streamlit app booted locally and returned rows for one query against
  the university demo database." No task report from Task 1 through
  Task 8, and no line in `docs/superpowers/sdd/progress.md`, records this
  ever being done. The Phase 1 spec's own definition of done (Task 9) is
  where this check first appears as a required step — it had not run when
  the Task 7 entry above claimed it as verified. Task 9 performed it for
  the first time today: `uv run streamlit run app.py --server.headless
  true --server.port 8501`, then `curl -s -o /dev/null -w "%{http_code}\n"
  http://localhost:8501` returned `200` with no traceback in the server
  log. Separately, the mocked end-to-end check (`ask_database("students
  per major", ...)` with `pipeline.generate_sql` patched) returned 5 rows
  with columns `['major', 'n']`. So the claim is true as of today, but the
  process that produced it in Task 7 was not: it was written in advance
  of the check it describes.

**Gate re-run today:** `uv run ruff check .` → `All checks passed!`.
`uv run ruff format --check .` → `44 files already formatted`.
`uv run mypy text_to_sql_agent` → `Success: no issues found in 13 source
files`. `uv run pytest` → `20 passed, 5 subtests passed`. Shim confirmed
absent (`text_to_sql_agent_mvp.py` does not exist); remaining hits for the
string `text_to_sql_agent_mvp` are the notebook's filename and historical
past-tense mentions in docs, not live code references.

**Also fixed:** `README.md`'s "Run Tests" section still told contributors
to run `python -m unittest discover -s tests`, stale since Task 2 moved
the suite to pytest and split it across fixtures unittest's discovery
cannot use. Replaced with `uv run pytest`.

## 2026-09-11 — Final whole-branch review: citation fix

A final review of this phase found that lines 47 and 74 above cite
`docs/superpowers/sdd/progress.md`. That path is wrong and cannot be opened
by a reader: the real file is `.superpowers/sdd/progress.md`, and
`.gitignore` deliberately excludes the `.superpowers/` directory as
subagent-driven-development controller scratch. Per the append-only rule
this note is added rather than editing lines 47 and 74 above. The durable
evidence for the claims those lines make is the commit history on
`tuannm3812/main-refinement` and the task reports from that phase, not the
`.superpowers/` path, which is intentionally not part of the committed
record.

## 2026-09-12 — Phase 2: correctness and seams

**Changed:** Replaced `safety.py`'s dangerous-keyword regex with an explicit
single-statement guard via `sqlglot.parse`, and its text-based schema-internals
check with an AST table-name check. Flipped the missing-`sqlglot` fallback from
fail-open to fail-closed. Made `execution.py`'s runaway-query abort return
`QUERY_ABORTED_AFTER_<n>_VM_STEPS` instead of escaping as a bare
`OperationalError`, identified by `exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT`;
renamed `DEFAULT_SQLITE_PROGRESS_STEPS` to `DEFAULT_MAX_VM_STEPS` and the keyword
`progress_steps` to `max_vm_steps`. Extracted `text_to_sql_agent/evaluation.py`
as the single copy of gold-vs-generated comparison. Split `app.py` from 759 lines
to 51, with the UI in nine `ui/` modules and the sidebar returning a frozen
`Settings` dataclass instead of communicating through `st.session_state` keys.
Extended `mypy` from `text_to_sql_agent` alone to `text_to_sql_agent`, `ui`,
`app.py` and `scripts`.

**Verified** (every figure below came from a command run while writing this entry,
on 2026-09-12):

- `uv run pytest` → `107 passed`. The phase began at 20.
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → `59 files already formatted`
- `uv run mypy` → `Success: no issues found in 26 source files`. Was 14 files.
- `uv run python -c "import app"` → clean.
- `uv run python scripts/evaluate_text_to_sql.py --mode gold` →
  `Evaluated 12 cases. Exact result match: 12/12`. The regenerated
  `evaluation/results/` differed from the committed copy **only** in `latency_ms`
  — checked column by column with `latency_ms` masked — so the churn was reverted.
- `wc -l app.py` → 51. `ls ui/*.py` → 10 files.
- The `retrieve_schema_context` body is 182 lines, so `docs/4_next_steps.md`'s
  previous "179-line" figure was corrected.
- The type gap this phase closed was demonstrated both ways: with
  `_ = backend.this_attribute_does_not_exist` appended to `app.py`, `ruff check`
  and `pytest` both **pass** and `mypy` reports
  `app.py:53: error: Module has no attribute ... [attr-defined]`. Before the
  widening, all three passed.
- **The rendered page is pixel-for-pixel unchanged by the UI split.** Both the
  pre-split commit `110e09f` and the current tree were served headless and
  screenshotted with Playwright/Chromium at 1400×1200 full-page; the two PNGs
  have the same SHA-256, `65aefe32d491e84f…`. A human (me) also looked at the
  rendered page: sidebar controls, the active-demo line, the sample-question
  selectbox, the welcome message and the chat input all render in the expected
  order with the chat CSS applied.

**Found by review, fixed:** an adversarial review of `safety.py` found that
`SELECT * FROM dbstat('main')` was **allowed and executed**, returning real
schema and page statistics — the table-valued-function branch checked only name
prefixes and never the `dbstat` name set. Quoted forms such as
`"pragma_table_info"('customers')` raised `AttributeError` rather than returning
`bool`. Both closed by matching on `Expression.name`. Separately, three tests
were found to pass for the wrong reason and were strengthened:
`test_repaired_sql_is_rechecked_for_safety` passed because the connection is
read-only rather than because the re-check runs; `rows_match`'s duplicate-row
handling was unpinned, so a `JOIN` fan-out would have scored as a match and
inflated accuracy; and `describe_error`'s two named-code messages could be
swapped without failing anything.

**Found, not fixed:** `safety.py` rejects a leading comment or a BOM before
`SELECT`. Recorded in `docs/4_next_steps.md` with the reason it is low impact.
`Settings.gemini_key` has no consumers. Both are listed there rather than fixed
here.

**Corrections to earlier claims:** the Phase 2 spec said `sqlite3` exposes no
error code distinguishing an interrupt — wrong for this project's Python floor;
3.11+ carries `sqlite_errorcode`. The spec also said deleting the safety regex
was sufficient; it was not, and `SELECT 1; SELECT 2` leaked under the *old* code
too, so the pre-existing hole was wider than the spec described. Both were
corrected in the plan before implementation.

**Open:** nothing from this phase is half-done. Phase 3 is next.
