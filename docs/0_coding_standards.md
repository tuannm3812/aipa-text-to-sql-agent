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

- **`line-length = 100`, not 79.** Master §3 asks for 79 "where practical". Here
  164 lines exceed 79 but only 43 exceed 100, and most of those in between are
  typed signatures and f-string report lines that read worse wrapped. Enforced by
  `ruff format`, so it is a ceiling and not a target.

- **`mypy --strict` over `text_to_sql_agent/`.** Neither the master standard nor
  any sibling project uses a type checker; master §3 asks for type hints without
  enforcing them. Adopted here because Phases 3 and 4 of the refactor rewrite
  that package heavily and strict typing pays for itself across a rewrite.
  `app.py`, `scripts/` and `tests/` are deliberately excluded.

- **No feature branches.** Single-owner project, so work commits directly to
  `tuannm3812/main-refinement` — this repo's default branch, not `main`. Master
  §10's pre-push workflow still applies in full. With no merge commits to
  summarise a phase, §9 commit hygiene carries the whole burden of a reviewable
  history.

- **`data/` exists deliberately.** Master §1 says to avoid it absent a real
  local-execution need. Six demo fixtures are tracked because `app.py` offers
  them as the built-in demo databases and `evaluation/cases.json` runs against
  them; the hosted demo is non-functional without them. `.gitignore` uses the
  master §8 ignore-then-negate pattern so the exception is visible rather than
  accidental. **Do not "tidy" this.**

- **The MVP notebook keeps its saved outputs.** Master §4 asks for outputs to be
  cleared or re-run when notebook code changes. `text_to_sql_agent_mvp.ipynb`
  keeps its 11 cells of output as point-in-time evidence for the academic
  deliverable in `docs/academic/`. A dated cell at the top says so. Any *code*
  change to the notebook still requires a re-run.

- **`E501` is off for `text_to_sql_agent/llm.py`.** Its long lines are all inside
  `SQL_TRANSLATION_SYSTEM_PROMPT`, where a line break is content the model reads.
  Rewrapping would change behaviour. Revisit if the prompt moves to its own file.

- **`requirements.txt` is generated**, by
  `uv export --no-hashes --no-dev --no-emit-project -o requirements.txt`. Edit
  `pyproject.toml` and re-export; never edit it directly. `tests/test_packaging.py`
  and CI both fail on drift. It cannot be deleted in favour of `pyproject.toml`
  alone because Streamlit Community Cloud and `.devcontainer/` read it.

## 3. Naming

- Retrieval results are `*RetrievalResult` dataclasses; schema units are `*Chunk`.
- Provider adapters are named for the provider (`gemini_manager.py`), not the
  vendor's model family.
- Error codes returned in `QueryResult.error` are `SCREAMING_SNAKE_CASE` string
  constants (`BLOCKED_UNSAFE_SQL`, `UNANSWERABLE_WITH_GIVEN_SCHEMA`), never
  free-form prose, because `app.py` and the evaluation harness branch on them.

## 4. Safety Conventions

- Generated SQL passes `is_safe_query` **and** executes under the read-only
  SQLite authorizer in `execution.py`. Neither is sufficient alone; never remove
  one because the other exists.
- The LLM receives schema metadata and low-cardinality value hints only. It never
  receives row data. Any change that would send rows to a provider needs an
  entry in `docs/3_decisions.md` first.
