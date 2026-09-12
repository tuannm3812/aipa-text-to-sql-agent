# Project Coding Standards — aipa-text-to-sql-agent

The shared baseline is the master standard at
`~/Documents/GitHub/coding-standards/coding_standards.md`. This file records
**only** what is specific to this project or deliberately different. Per master
§13 it must never restate the master; if a rule here also appears there, delete
it here.

## 1. Doc shape

**Shape B** (app/product), per master §2. The repo's centre of gravity is the
Streamlit agent and its backend package. The offline evaluation harness measures
that agent rather than being the subject of the repo, and nothing is trained, so
Shape A's EDA-then-baseline sequence does not describe this work.

Renumbering an existing repo is normally forbidden by §2. It was done here on
2026-09-11 under the same section's exemption for "repos being substantially
reworked anyway" — see `docs/superpowers/specs/2026-09-10-refactor-roadmap.md`.

## 2. Deltas from the master

- **`line-length = 100`, not 79.** Master §3 asks for 79 "where practical". When
  this was decided, 164 lines exceeded 79 but only 43 exceeded 100, and most of
  the ones in between were typed signatures and f-string report lines that read
  worse wrapped. After `ruff format` ran at width 100, 179 lines exceed 79 and
  only 10 exceed 100 — all of them inside `SQL_TRANSLATION_SYSTEM_PROMPT` in
  `text_to_sql_agent/llm.py`, which is `E501`-excluded because those lines are
  prompt content rather than code. Enforced by `ruff format`, so 100 is a ceiling
  and not a target. Same delta `ai-meal-planner` took, for the same reason.

- **`mypy --strict` over `text_to_sql_agent/`.** No sibling project *following
  this standard* uses a type checker; master §3 asks for type hints without
  enforcing them. Adopted here because Phases 3 and 4 of the refactor rewrite
  that package heavily and strict typing pays for itself across a rewrite.
  `tests/` is deliberately excluded. (Phase 2 brought `app.py`, `ui/` and
  `scripts/` into scope at a lower strictness — see the next bullet.)

- **Two-tier mypy strictness.** As of Phase 2, `mypy` also covers `app.py`,
  `ui/` and `scripts/` (26 files total, up from 14), not just
  `text_to_sql_agent/`. `text_to_sql_agent` stays `strict = true`. The other
  three relax exactly two settings from strict —
  `disallow_any_generics` and `warn_return_any` — because Streamlit's
  decorated API returns `Any` in places strict mode cannot resolve without
  casts that document nothing, and its generic containers are often
  unparameterised. Everything else, including `disallow_incomplete_defs`,
  stays on: a new UI function still needs full annotations, and attribute
  access against the backend package is still checked, which is what closes
  the gap a rename in `__init__.py`'s `__all__` used to leave open. See
  `docs/3_decisions.md` for the full rationale.

- **`ui/` sits beside `app.py`, not inside `text_to_sql_agent/`, and is not
  packaged.** Presentation code (Streamlit layout, widgets, session-state
  wiring) does not belong in the backend package that the notebook and the
  evaluation script import independently of Streamlit.
  `[tool.hatch.build.targets.wheel] packages` lists only
  `["text_to_sql_agent"]`; `ui/` is deliberately absent so the wheel never
  pulls in a Streamlit-dependent module for a consumer that only wants the
  backend. See `docs/3_decisions.md`.

- **No feature branches.** Single-owner project, so work commits directly to
  `tuannm3812/main-refinement` — this repo's default branch, not `main`. Master
  §10's pre-push workflow still applies in full. With no merge commits to
  summarise a phase, §9 commit hygiene carries the whole burden of a reviewable
  history.

- **`data/` exists deliberately.** Master §1 says to avoid it absent a real
  local-execution need. Six demo fixtures are tracked, but they are not all
  load-bearing for the same reason. Three are: `university_agent.db`,
  `retail_analytics.db`, and `healthcare_analytics.db` are exactly the three
  databases `app.py`'s `DEMO_DATABASES` offers, and the same three back all
  twelve cases in `evaluation/cases.json`; the hosted demo is non-functional
  without them. The other three are illustrative rather than referenced by
  code: `customers.csv` and `sales.csv` are small sample files for the
  CSV-upload path, and `dynamic_agent.db` is not itself read anywhere — it
  matches the *filename* `ingestion.ingest_csvs_to_db` writes to by default,
  showing a contributor what that output looks like. `.gitignore` uses the
  master §8 ignore-then-negate pattern so the exception is visible rather than
  accidental. **Do not "tidy" this** — keep all six tracked and keep the
  negations, even though only three are load-bearing.

- **The MVP notebook keeps its saved outputs.** Master §4 asks for outputs to be
  cleared or re-run when notebook code changes. `text_to_sql_agent_mvp.ipynb`
  keeps its 11 cells of output as point-in-time evidence for the academic
  deliverable in `docs/academic/`. A dated cell at the top says so. Any *code*
  change to the notebook still requires a re-run. `[tool.ruff.format] exclude`
  only stops reformatting; `ruff check --fix` is a separate path and can still
  rewrite the notebook's cells (and would if fix-on-save is enabled in an
  editor), silently invalidating the saved outputs. The notebook is only safe
  today because it happens to be lint-clean; this is not configured away,
  only documented.

- **`[tool.ruff] force-exclude = true`, plus `[tool.ruff.format] exclude` for
  the notebook.** Ruff ignores its own `exclude`/`format.exclude` when a path
  is passed explicitly on the command line, which an editor's format-on-save
  or a pre-commit hook does. `force-exclude` makes the notebook's format
  exclusion hold even then, so an editor cannot silently reformat
  `text_to_sql_agent_mvp.ipynb` and invalidate its saved outputs. It does not
  cover `ruff check --fix`; see the notebook bullet above.

- **`E501` is off for `text_to_sql_agent/llm.py`.** Its long lines are all inside
  `SQL_TRANSLATION_SYSTEM_PROMPT`, where a line break is content the model reads.
  Rewrapping would change behaviour. Revisit if the prompt moves to its own file.

- **`requirements.txt` is generated**, by
  `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`. Edit
  `pyproject.toml` and re-export; never edit it directly. `tests/test_packaging.py`
  and CI both fail on drift. It cannot be deleted in favour of `pyproject.toml`
  alone because Streamlit Community Cloud reads it. `.devcontainer/` no
  longer does — commit `bdb1eca`, later in this same phase, rewrote its
  `updateContentCommand` to `pip3 install --user uv && uv sync`, so
  `requirements.txt` is kept for the Streamlit Cloud half of this reason
  alone.

## 3. Naming

- Retrieval results are `*RetrievalResult` dataclasses; schema units are `*Chunk`.
- Provider adapters are named for the provider (`gemini_manager.py`), not the
  vendor's model family.
- Error codes returned in `QueryResult.error` are `SCREAMING_SNAKE_CASE` string
  constants (`BLOCKED_UNSAFE_SQL`, `UNANSWERABLE_WITH_GIVEN_SCHEMA`), never
  free-form prose, because `app.py` and the evaluation harness branch on them.
  The full set is `BLOCKED_UNSAFE_SQL`, `UNANSWERABLE_WITH_GIVEN_SCHEMA`,
  `RESULT_TRUNCATED_TO_<n>_ROWS` and `QUERY_ABORTED_AFTER_<n>_VM_STEPS`.
  `ui/results.py`'s `describe_error` maps each to a message a non-technical
  reader can act on; anything unrecognised passes through unchanged, because
  the backend also puts raw exception text in that field.

## 4. Safety Conventions

- Generated SQL passes `is_safe_query` **and** executes under the read-only
  SQLite authorizer in `execution.py`. Neither is sufficient alone; never remove
  one because the other exists.
- The LLM receives schema metadata and low-cardinality value hints only. It never
  receives row data. Any change that would send rows to a provider needs an
  entry in `docs/3_decisions.md` first.
