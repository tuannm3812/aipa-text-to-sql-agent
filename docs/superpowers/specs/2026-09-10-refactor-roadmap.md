# Refactor & Improvement Roadmap

**Date:** 2026-09-10
**Status:** Approved 2026-09-11
**Scope:** Whole repository, delivered as five sequential phases
**Baseline standard:** `~/Documents/GitHub/coding-standards/coding_standards.md`

## Context

`aipa-text-to-sql-agent` is a working Streamlit prototype that translates natural
language into read-only SQLite queries. The backend package `text_to_sql_agent/`
is already modular. The weak points are the 736-line `app.py`, an unmeasurable
RAG layer, duplicated evaluation logic, and the absence of project tooling and
agent instructions.

The goal is to take the project as far as it can reasonably go, in phases ordered
from low-risk to ambitious. Nothing in the current repo is treated as sacred: the
compatibility shim, the package layout, the doc numbering, and the docs
themselves may all change.

The repo predates the master coding standard, so Phase 1 also brings it into
alignment. Master §2 forbids renumbering an existing repo purely for tidiness but
explicitly permits it for "repos being substantially reworked anyway" — which is
what this roadmap is.

## Doc shape

**Shape B** (app/product), per master §2. The repo's centre of gravity is the
Streamlit agent and its backend package; the offline evaluation harness measures
that agent rather than being the subject of the repo, and nothing is trained.
Recorded in `docs/0_coding_standards.md`.

## Known defects driving the plan

Line counts and figures below were measured on 2026-09-10, before any phase ran.
Phase 1's `ruff format` pass changed some of them — `app.py` grew from 736 to 759
lines when long lines were wrapped at width 100. `docs/4_next_steps.md` carries
the current figures; this table is the historical record that motivated the plan.

| # | Location | Defect |
|---|----------|--------|
| 1 | `text_to_sql_agent/safety.py:12-27` | The dangerous-keyword regex rejects legitimate SQL. `REPLACE(...)` is a standard SQLite string function, and any string literal containing `update`, `delete`, or `create` trips the same check. The sqlglot AST check on the following lines already enforces read-only correctly. |
| 2 | `text_to_sql_agent/execution.py:71` | `conn.set_progress_handler(lambda: 1, progress_steps)` returns a truthy value, so SQLite aborts the query every 100,000 VM steps. Whether this is an intentional runaway-query guard or a bug, it is undocumented and surfaces as a bare `OperationalError: interrupted`. |
| 3 | `text_to_sql_agent/rag.py:225` | `_hashed_embedding(chunk.search_text)` is recomputed for every chunk on every question, with no caching. It is also built from the same character n-grams as the semantic score on the line above, so the two signals are largely redundant. |
| 4 | `text_to_sql_agent/rag.py:151-283` | `retrieve_schema_context` is a single 130-line function. Scoring constants (`8.0`, `4.0`, `2.5`, `1.5`, `0.75`, `0.35`) are hardcoded inline while sibling weights live in `config.py`. No individual signal can be tuned or unit-tested. |
| 5 | `app.py:261-345` and `scripts/evaluate_text_to_sql.py:25-160` | Evaluation row-normalisation and matching logic is duplicated verbatim across two files. |
| 6 | `app.py:388-733` | `main()` is roughly 345 lines covering secrets, uploads, sidebar, chat, charting, and the evaluation tab. |
| 7 | Repository root | No `pyproject.toml`. Dependencies are declared with `>=` only. No linter or formatter. CI runs `unittest` on a single Python version. |
| 8 | Repository root | No `AGENTS.md` and no `CLAUDE.md`, so no session in this repo loads the master standard (master §13, layer 2 missing entirely). |
| 9 | `.gitignore` | No rule covers `data/`. Six demo fixtures totalling ~212 kB are tracked with nothing marking them as a deliberate exception, so master §8 cannot be audited here. |
| 10 | `docs/` | Uses ad hoc `academic/` and `supporting/` folders rather than the Shape B numbering of master §2. |
| 11 | `text_to_sql_agent_mvp.py` vs `text_to_sql_agent/pipeline.py` | The "compatibility shim" is a byte-identical copy of `pipeline.py` apart from its module docstring and the three function docstrings — about 170 duplicated lines. The tests patch and exercise the shim's copy, so `pipeline.py`'s `ask_database` has no coverage at all, and a fix applied there would not reach the code under test. Verified 2026-09-11. |

## Phases

Each phase gets its own design spec, implementation plan, and merge. Commits
follow Conventional Commits per master §9, and every phase ends with the master
§10 pre-push workflow.

### Phase 1 — Foundation and standards alignment
Project tooling, agent instructions, doc reshaping, and test structure. No
behaviour change. Addresses defects 7, 8, 9, 10, and 11.
Detailed spec: `2026-09-10-phase-1-foundation-design.md`.

### Phase 2 — Correctness and seams
Fixes defects 1, 2, 5, and 6. Removes the safety false positive, makes the query
abort behaviour explicit and typed, extracts a shared evaluation module consumed
by both the app and the CLI script, and splits `app.py` into focused UI modules
behind a thin entrypoint. Every fix ships with a regression test.

### Phase 3 — Engine abstraction
Introduces a dialect/engine protocol behind `schema.py`, `execution.py` and
`safety.py`. SQLite becomes one implementation, with **DuckDB and PostgreSQL both
delivered in this phase** (owner's decision, 2026-09-14), split into plans 3a and
3b so PostgreSQL's infrastructure does not block DuckDB. This precedes the RAG
work deliberately, because schema chunking is engine-specific and doing RAG first
would mean rewriting it.

The governing constraint is that the safety model does not port: SQLite's
read-only guarantee is a URI flag plus a 28-constant authorizer, and neither
DuckDB nor PostgreSQL has anything shaped like it. Each engine proves read-only
its own way against a shared conformance suite. Detailed spec:
`2026-09-14-phase-3-engine-abstraction-design.md`.

### Phase 4 — Real RAG
Fixes defects 3 and 4. Decomposes scoring into named, individually testable
signal functions with weights in config. Replaces the hashed pseudo-embedding
with real sentence embeddings behind an optional dependency group, falling back
to the lexical path so the hosted demo stays lightweight — the same
`[project.optional-dependencies]` pattern `ai-meal-planner` uses for
`semantic-rag`. Adds a persistent index keyed by schema hash, and retrieval
metrics (recall@k, MRR) to the evaluation harness so the claims in the academic
report become measurable.

### Phase 5 — Agent loop
Replaces the linear pipeline with plan → retrieve → generate → self-critique →
execute → repair, exposing a structured trace in the UI. The current single-shot
repair in `pipeline.py` becomes a degenerate case of the loop.

## Dependencies between phases

- Phase 2 depends on Phase 1 (tests and lint gate the refactor).
- Phase 4 depends on Phase 3 (engine-specific schema chunking).
- Phase 5 depends on Phase 2 (needs the extracted seams).
- Phase 3 and Phase 4 are independent of Phase 5's UI work.

## Evidence locations

- `docs/3_decisions.md` — dated decision log; any architectural claim traces here
- `docs/6_agent_log.md` — append-only record of agent work and what was verified
- `evaluation/results/` — measured accuracy per provider and model
