# Evaluation Contract v2 — Spider, BIRD and a safety suite

**Date:** 2026-10-08
**Status:** Awaiting review
**Parent:** the production-readiness direction, `docs/6_agent_log.md`
(2026-10-07 and 2026-10-08 portfolio-session entries); gate **G2**
**Baseline standard:** `~/Documents/GitHub/coding-standards/coding_standards.md`

## 1. Purpose

Freeze what an evaluation result means before any more results are produced,
then run two public text-to-SQL benchmarks through that contract.

Today the harness scores 12 hand-written cases against three demo databases,
and the README's model tables are a May snapshot of a pipeline that no longer
exists (labelled as such since `2c75e46`). The direction asks for typed
expectations per case, a separate safety metric so a generic error never
counts as a correct refusal, a manifest recording exactly what was measured,
confidence intervals on every rate, and v2 numbers that are never presented as
an improvement over the 12-case tables.

The owner's decisions on 2026-10-08, recorded here so the plan does not
relitigate them:

- **Public benchmarks:** Spider 1.0 dev and BIRD dev, both included.
- **BIRD evidence:** report both — evidence appended to the question
  (comparable to published numbers) and question only (what a user of this
  app gets).
- **Run policy:** committed, seeded, hardness-stratified subsets for routine
  runs and the CI regression gate; the full dev sets for dated release runs.
- **Architecture:** one normalised case format with per-benchmark adapters
  (approach A); not wrappers around the benchmarks' own scorers, not flags
  bolted onto the existing script.

## 2. Non-goals

- No new evaluation *cases* written by hand beyond the small safety suite.
  Authoring 50+ analytics questions is unnecessary once Spider and BIRD are in.
- No RAG on/off ablation. That is gate G8 and runs on the contract this spec
  freezes.
- No change to `safety.py`, the engines, or the prompt. If a benchmark exposes
  a validator defect, it is logged for a separate fix, not patched here.
- No benchmark data committed to the repository. Spider is ~100 MB of
  databases, BIRD dev several times that.
- No hosted-provider run in CI.
- No claim of leaderboard comparability. Our scorer is documented and
  comparisons are within this harness.

## 3. Findings this spec acts on

Checked on 2026-10-08 against `d34aba8`.

- `evaluation/cases.json` holds 12 cases with `id`, `dataset`, `db_path`,
  `question`, `gold_sql`, `expected_tables`, `difficulty` — no notion of a case
  whose correct outcome is a refusal or "unanswerable".
- `text_to_sql_agent/evaluation.py`'s `score_case` requires both sides to have
  executed before any match counts, so a blocked query cannot inflate
  accuracy. That guard is kept; v2 wraps it.
- `scripts/evaluate_text_to_sql.py` writes one CSV and one Markdown file per
  provider/model into `evaluation/results/`, overwriting previous runs, with
  no record of commit, prompt, scorer or configuration. Row-level errors are
  already DSN-redacted (`a280fde`).
- `ui/evaluation.py` re-runs every case live inside Streamlit and scores with
  the same `score_case`, so UI and CLI agree today; the plan keeps that true.
- The Spider dev set is 1,034 questions over 20 SQLite databases with
  `easy/medium/hard/extra` labels, CC BY-SA 4.0. The BIRD dev set is 1,534
  question–SQL pairs over 11 SQLite databases with a per-question `evidence`
  string and `simple/moderate/challenging` labels, CC BY-NC(-SA) 4.0. Both run
  on the existing `SQLiteEngine` with no new infrastructure.

## 4. Design

### 4.1 The case contract

Every case, from every source, is one record:

| Field | Meaning |
|---|---|
| `suite` | `spider_dev`, `bird_dev`, `safety`, `demo` |
| `id` | stable and unique within the suite — the benchmark's own index for public suites, a slug for ours |
| `db_path` | the SQLite file; repo-relative for ours, under `data/benchmarks/` for public suites |
| `question` | as a user would type it |
| `evidence` | BIRD's hint, else `""`; appended to the question only when a run is configured `evidence=on` |
| `gold_sql` | the reference query; `""` when `expected` is not `answerable` |
| `hardness` | `easy` / `medium` / `hard` / `extra`; BIRD's `simple/moderate/challenging` map to `easy/medium/hard` |
| `expected` | `answerable`, `expect_refusal`, `expect_unanswerable` |
| `expected_tables` | tables a correct answer needs, for schema recall; may be empty |

Rules:

- Records are validated on load (`load_suite` raises on a missing or ill-typed
  field), so a broken conversion fails before a run rather than scoring zero.
- A case whose `expected` is not `answerable` carries no gold SQL and is never
  row-compared.
- The existing 12 cases become suite `demo` in this shape. `evaluation/cases.json`
  is left untouched as history, and `load_cases` keeps working for anything
  that still calls it.
- Suites are JSON Lines files in `evaluation/suites/`. Ours (`safety.jsonl`,
  `demo.jsonl`) are tracked; the public ones are generated and ignored.

### 4.2 Adapters and download

`scripts/prepare_benchmarks.py` has one adapter per public suite and no other
responsibility:

- **Spider**: fetch the dev release, unpack its databases to
  `data/benchmarks/spider/`, write `evaluation/suites/spider_dev.jsonl`.
- **BIRD**: fetch the dev release (`dev.json` plus `dev_databases`), unpack to
  `data/benchmarks/bird/`, write `evaluation/suites/bird_dev.jsonl` with
  `evidence` filled.
- Each adapter records the archive's SHA-256 and the release identifier into
  `evaluation/suites/<suite>.source.json`, which the manifest copies.

`data/benchmarks/` and the two generated `.jsonl` files are gitignored; the
`.source.json` files are tracked so a run can be tied to a release even when
the data is absent.

**Subsets** are explicit ID lists, `evaluation/suites/<suite>.subset200.txt`,
drawn once with a fixed seed and stratified by hardness in the suite's own
proportions, then committed. A run names the list it used; the list is never
re-drawn. The draw script and its seed are kept so the list is reproducible,
but reproducibility is a check, not a workflow.

Both public suites run on `SQLiteEngine`, so every benchmark query — gold SQL
included — goes through `is_safe_query` and the read-only authorizer exactly
as a user's would. A gold query that fails the safety check is reported as
`GOLD_SQL_UNSAFE`, counted, and listed in the report; it is never silently
dropped and never executed.

### 4.3 Scorer and metrics

`score_case` stays the row-comparison core. A new `score_v2(case, result,
gold_result) -> Outcome` wraps it with the typed expectation:

| `expected` | Outcome |
|---|---|
| `answerable` | `correct` (value match), `wrong` (executed, no match), `error` (did not execute), `refused` (a blocking code), `unanswerable` (the sentinel) |
| `expect_refusal` | `correct` only if `result.error` is `BLOCKED_UNSAFE_SQL` or `BLOCKED_UNSUPPORTED_COLUMN_TYPE`; everything else `wrong` |
| `expect_unanswerable` | `correct` only on `UNANSWERABLE_WITH_GIVEN_SCHEMA`; everything else `wrong` |

A generic exception is never a correct refusal.

Reported per run, overall and per hardness:

- **Execution accuracy (EX):** `correct` over answerable cases. The headline.
- **Safety accuracy:** `correct` over refusal and unanswerable cases. Separate,
  so it cannot pad EX.
- **False-refusal rate:** answerable cases scored `refused`. The visible cost of
  default-deny.
- **Schema recall@k** on `expected_tables`, as today.
- **Latency**, and **token counts** where the provider reports them.

Every rate carries a **bootstrap 95% confidence interval**: 10,000 resamples
over cases, seed fixed, percentile method. Implemented in
`text_to_sql_agent/evaluation.py` with no new dependency (`random` and the
standard library suffice; NumPy is not added for one function).

### 4.4 Manifest and result files

One run writes one directory,
`evaluation/results/<YYYY-MM-DD>_<suite>_<provider>_<model>[_<tag>]/`:

- `manifest.json`: repo commit, suite and subset list (or `full`), the source
  archive hash and release, scorer version (a constant in `evaluation.py`
  bumped on any scoring change), prompt hash (SHA-256 of the assembled system
  prompt for the engine), provider, model, `use_rag`, `rag_top_k`,
  `evidence`, `work_limit`, `max_rows`, start time, duration, Python and
  package versions.
- `cases.csv`: one row per case — outcome, generated SQL, error code
  (DSN-redacted), latency, tokens.
- `report.md`: the metrics with intervals, overall and per hardness, and the
  manifest's identifying fields as a header.

Rules: a result is never cited without its manifest; the README's tables carry
date and commit and link to the directory; `report.md` states that v2 numbers
are not comparable to the May tables; the old files stay where they are,
labelled historical.

The gold baseline keeps its role as a gate. `--mode gold` runs no model, so
it checks two different things: every answerable case's gold SQL executes and
scores `correct` against itself (100 % EX), and every non-answerable case is
well-formed — no gold SQL, and an `expected` whose correct code is one the
pipeline can actually produce. Safety *accuracy* is a model metric and is
reported only for model runs.

### 4.5 Run policy and the CI gate

- **Routine:** `spider_dev.subset200`, `bird_dev.subset200` twice (evidence on
  and off), `safety` and `demo` in full — about 600 LLM calls.
- **Release:** the full dev sets, once per version, producing the dated
  results the README cites.
- **CI gate (every push, no provider, no network):** `--mode gold` over every
  suite whose data is present. `safety` and `demo` always run; the public
  subsets run only when the archives are cached on the runner and otherwise
  skip with an explicit reason, the same pattern as the PostgreSQL tests. Gold
  must score 100 % EX on answerable cases and every non-answerable case must
  pass the structural check. This proves the harness, not the model.
- **Accuracy regression gate (on demand, needs a model):** compares a new
  subset run's EX with the last committed report for the same suite,
  provider, model and evidence setting, and fails when the drop exceeds the
  width of the new run's interval. Definition of done includes observing it
  fail on a deliberately broken prompt and pass on the fix.

### 4.6 The Streamlit evaluation tab

The tab keeps its live run for the `demo` suite — twelve cases are quick and
it is the demo's point — and gains a read-only view of any committed result
directory, rendered from `report.md` and `cases.csv`. The live run goes
through `score_v2` so the tab and the CLI cannot disagree about an outcome,
which is the Phase 2 lesson kept.

### 4.7 Testing

Tests live in `tests/test_evaluation.py` (extended) and
`tests/test_evaluation_v2.py` (new), and need no network and no benchmark data:

- Contract: `load_suite` accepts a valid record and rejects each missing or
  ill-typed field with a message naming it; a non-answerable case with gold SQL
  is rejected.
- Scorer: one test per outcome row in §4.3's table, plus the two negative rules
  — a generic error is `wrong` for a refusal case, and a refusal is `refused`
  (never `correct`) for an answerable case.
- Intervals: the bootstrap is deterministic under its seed; a rate of 0/n and
  n/n yields a degenerate interval; the interval narrows as n grows.
- Adapters: each converts a hand-written three-record fixture in the
  benchmark's own format to the contract, including BIRD's hardness mapping
  and evidence; the subset draw is reproducible from its seed and stratified.
- Manifest: a run against the `demo` suite produces all three files, the
  manifest's commit matches `git rev-parse HEAD`, and the prompt hash matches
  the pinned SQLite prompt SHA-256 from `tests/test_llm.py`.
- Gate: the gold gate passes on `demo` and `safety`; it fails when an
  answerable case's gold SQL is corrupted, and when a safety case is given an
  `expected` value the contract does not define.
- Regression gate: fails on a synthetic report pair with a drop wider than the
  interval, passes on one within it.
- The May result files are untouched: a test asserts their contents' hashes.

## 5. Risks

- **Benchmark downloads move or change.** The adapters record the archive
  hash, and a hash mismatch is a hard failure with the recorded value in the
  message, so a silently different release cannot produce comparable-looking
  numbers.
- **BIRD gold SQL that our validator refuses.** Expected for a handful of
  cases; reported as `GOLD_SQL_UNSAFE` and excluded from EX's denominator with
  the count stated, never hidden. Each one is also a free probe of the
  validator and is logged for review.
- **Large schemas overwhelm the n-gram RAG.** Likely on BIRD. That is a
  finding for Phase 4, not something this spec tunes around; `rag_top_k` is in
  the manifest so it can be varied deliberately.
- **Subset results are mistaken for full results.** The manifest and report
  header name the subset list; the README cites full-set runs only.

## 6. Definition of done

```
uv run python scripts/prepare_benchmarks.py --suite spider_dev bird_dev
uv run pytest                                       # v2 tests included, no network
uv run python scripts/evaluate_v2.py --suite demo safety --mode gold   # 100 % EX, structural check clean
uv run python scripts/evaluate_v2.py --suite spider_dev --subset subset200 \
    --provider ollama --model llama3:latest           # a dated result directory
uv run python scripts/evaluate_v2.py --suite bird_dev --subset subset200 \
    --provider ollama --model llama3:latest --evidence on   # and off
```

CI green with the gold gate running; the regression gate observed failing on
a deliberate regression and passing after; a decision entry recording the
contract, the two benchmarks, the evidence policy and the run policy; the
README's evaluation section pointing at dated v2 result directories and
keeping the May tables labelled historical.
