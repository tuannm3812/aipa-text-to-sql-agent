# aipa-text-to-sql-agent

A Streamlit decision-support agent that translates natural-language questions
into safe, read-only SQLite queries, grounding the LLM with a local hybrid schema
RAG layer that indexes DDL and column metadata but never row data.

It is **not** a modelling repo — nothing is trained, and the `evaluation/`
harness measures the agent rather than a model. Do not confuse it with the
sibling `ai-meal-planner`, which is a FastAPI service.

## Standards

Follow the master standard at `~/Documents/GitHub/coding-standards/`.
Project-specific rules and deliberate overrides: @docs/0_coding_standards.md

## Deltas from the master

- `line-length = 100`, not 79 — see `docs/0_coding_standards.md` §2 for the count.
- `mypy --strict` over `text_to_sql_agent/` only. No sibling project following
  this standard uses a type checker — the master standard asks for type hints
  without enforcing them; this one does because Phases 3-4 rewrite that
  package heavily.
- **No feature branches.** Commit directly to `tuannm3812/main-refinement`, which
  is this repo's default branch — not `main`.
- `data/` and the notebook's saved outputs are deliberate §8 and §4 exceptions.
  **Do not "tidy" either.**
- `requirements.txt` is **generated** by `uv export`. Edit `pyproject.toml` and
  re-export; CI and `tests/test_packaging.py` both fail on drift.

## Evidence locations

- `docs/3_decisions.md` — dated decision log; architectural claims trace here
- `docs/6_agent_log.md` — append-only record of agent work and what was verified
- `docs/superpowers/specs/` — the five-phase refactor roadmap and per-phase specs
- `evaluation/results/` — measured accuracy per provider and model

## Current state

- 2026-09-11: Phase 1 (foundation and standards alignment) complete. Baseline is
  20 passing tests. CI has two jobs: `test` runs `pytest` on a 3.11-3.13
  matrix; `quality` runs ruff, mypy, and the packaging drift guard once,
  unmatrixed. Phases 2-5 are specced in `docs/4_next_steps.md`.
- 2026-09-19: Phase 3a (engine protocol, SQLite port, DuckDB) complete. `439`
  tests pass with `uv run pytest` (`uv sync --extra engines` first); the
  engine conformance suite (`uv run pytest -m conformance -v`) runs `24`
  tests, `12` per engine (SQLite, DuckDB), `0` skipped. Both CI jobs now
  `uv sync --extra engines` and `test` fails the build if any conformance
  test is skipped. `duckdb` stays out of `requirements.txt` — it is an
  optional extra.
- 2026-09-26: Phase 3b (PostgreSQL as the third engine, schema-qualified
  table identity carried through all three) complete. `750` tests pass with
  `uv run pytest` (`uv sync --extra engines` first, `AIPA_TEST_POSTGRES_DSN`
  set against a live `docker compose -f docker/postgres.yml up -d`
  container); the 6 skips left even with the DSN set are a deliberate SQLite
  exemption (`ATTACH` is denied), not a gap. Without the DSN, the same
  command reports `503` passed, `253` skipped. The engine conformance suite
  (`uv run pytest -m conformance -rs`) runs `36` tests, `12` per engine
  (SQLite, DuckDB, PostgreSQL), `0` skipped. CI runs a `postgres:16` service
  container and fails the build if any conformance test is skipped, the same
  guard Phase 3a added for `duckdb`. `psycopg` stays out of
  `requirements.txt`, like `duckdb` — both are optional extras (`postgres`,
  or `engines` for both together). Phase 4 (real RAG) is next; see
  `docs/4_next_steps.md`.

## Open risks

- The RAG "embedding" in `rag.py` is a hashed bag of character n-grams, not a
  semantic embedding, and is recomputed per chunk per question with no cache. It
  largely duplicates the semantic signal beside it. Phase 4.
