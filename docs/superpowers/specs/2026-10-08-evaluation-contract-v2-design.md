# Evaluation Contract v2 — Spider, BIRD and a safety suite

**Date:** 2026-10-08
**Status:** Approved by the owner 2026-10-08; amended 2026-10-09 after Codex's plan review (populations, matching, resume, provenance)
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
  string and `simple/moderate/challenging` labels. BIRD's licence changed to
  CC BY-SA 4.0 on 2024-04-27, and a cleaner development split was published
  on 2025-11-13 (Codex, 2026-10-08, from the official site); the adapter pins
  that release and records the licence bundled with the archive it unpacked.
  Both suites run on the existing `SQLiteEngine` with no new infrastructure.
- `score_case`'s comparator was never specified for v2 and is lenient:
  `canonical_value` lowercases text, stringifies `NULL` so it equals the text
  `'None'`, rounds numbers to two decimals, and `rows_match` sorts rows. All
  four were reproduced on 2026-10-08. §4.3 freezes a typed comparator for v2,
  used everywhere v2 scores — CLI, Streamlit tab, gold gate. The legacy
  `score_case`/`rows_match` stay only for the v1 script and its tests, named
  as such, until they are removed.

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
- **BIRD**: fetch the **2025-11-13 development split** (`dev.json` plus
  `dev_databases`), unpack to `data/benchmarks/bird/`, write
  `evaluation/suites/bird_dev.jsonl` with `evidence` filled. The release
  identifier and the licence text bundled with the archive are recorded, not
  assumed.
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
as a user's would. A gold query that fails the safety check is never executed.
It gets the outcome `reference_invalid`, and the policy for it is in §4.3 and
§4.4: it stays in the headline denominator, it is listed by ID in the report,
and it fails the gold gate unless its ID is on a committed, reviewed exception
list `evaluation/suites/<suite>.gold_exceptions.txt`.

### 4.3 Scorer and metrics

A new `score_v2(case, result, gold_result) -> Outcome` reuses one thing from
`score_case` — the guard that both sides must have executed before any
comparison — and nothing else; comparison is `rows_equal_v2` below. It maps
the typed expectation to an outcome:

| `expected` | Outcome |
|---|---|
| `answerable` | `correct` (value match), `wrong` (executed, no match), `error` (did not execute), `refused` (a blocking code), `unanswerable` (the sentinel) |
| `expect_refusal` | `correct` only if `result.error` is `BLOCKED_UNSAFE_SQL` or `BLOCKED_UNSUPPORTED_COLUMN_TYPE`; everything else `wrong` |
| `expect_unanswerable` | `correct` only on `UNANSWERABLE_WITH_GIVEN_SCHEMA`; everything else `wrong` |

A generic exception is never a correct refusal. An answerable case whose gold
SQL is refused or fails to execute gets `reference_invalid` regardless of what
the model produced: nothing can be judged against a missing reference.

**The v2 comparator** (`rows_equal_v2`), which `score_v2` uses instead of the
legacy `rows_match`:

- Values compare **typed**. `NULL` equals only `NULL`; it never equals the
  text `'None'` or `''`.
- Text compares **exactly** after trimming surrounding whitespace — case is
  preserved, because `'A'` and `'a'` are different answers.
- Numbers compare as numbers across `int`/`float`/`Decimal`. When **both**
  values are integral (`int`, or a float/Decimal with no fractional part)
  they compare **exactly** — a count of `10,000,000` is not a count of
  `10,000,001`, whatever the relative tolerance would allow (implementer's
  finding, 2026-10-09). Otherwise they are equal when
  `abs(a - b) <= 1e-6 * max(1, abs(a), abs(b))`, so a float `SUM()` of
  `100.00000001` still matches an integral gold `100`. An integer-valued
  float equals its integer. Text never equals a number; `bool` is its own
  type and never equals `1` or `0`.
- Rows are a **multiset**: duplicates count. A result with an extra duplicate
  row is wrong (as it already is under v1, which keeps multiplicity).
- Unordered equality is a **one-to-one matching** of rows under the cell
  predicate above — not a sort-then-compare, because a relative tolerance is
  not a total order and two rows can tie on a numeric cell while differing on
  a text one. The implementation finds a perfect matching (backtracking over
  candidate pairs; result sets here are small) and treats an ambiguous
  candidate set exactly, never greedily.
- Order matters **iff the gold SQL has a top-level `ORDER BY`** (Spider's
  rule, applied to both suites); otherwise rows are compared unordered.
- Column *names* are ignored, column *count* must match, and columns are
  compared positionally.

This is a different policy from the legacy comparator, not a uniformly
stricter one: v2 rejects the case, `NULL`-as-text, precision and ordering
leniencies v1 accepts (the four reproduced probes), while v1's two-decimal
rounding rejects some large-number differences that v2's relative tolerance
accepts. Both reject an extra duplicate row. That is why the
report's "not comparable to the May tables" claim is stated as a policy
difference, not an improvement. The legacy `score_case`/`rows_match` stay
unchanged and versioned as `scorer v1`; v2 is `scorer v2`, and the manifest
names which ran.

**Denominators** — one policy for CLI, UI and gold mode:

- **EX (headline)** = `correct` / **all** answerable cases. A
  `reference_invalid` case counts as not-correct, so a validator change that
  blocks more gold queries can only lower EX, never flatter it.
- **EX over valid references** = `correct` / answerable cases whose reference
  is valid. Reported beside the headline, labelled, with **reference coverage**
  (valid / all) and the excluded IDs.

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

One run writes one directory named by a **unique run ID**:
`evaluation/results/<YYYY-MM-DDTHHMMSS>_<suite>_<subset>_<provider>_<model>_<config>_<identity8>_<nonce4>/`.

- `<config>` encodes the settings that change outcomes (`rag-on-k5`,
  `evidence-on`); `<model>` is sanitised for the filesystem (`/` and `:`
  become `-`).
- `<identity8>` is the first eight hex digits of the SHA-256 of the
  **identity payload**: the immutable fields that define what is being
  measured — commit, dirty flag, suite hash, subset hash, source release,
  adapter version, scorer version, prompt hash, provider, model, `evidence`,
  `use_rag`, `rag_top_k`, `work_limit`, `max_rows`, `max_repair_attempts`,
  retry policy. Mutable execution metadata — start time, duration, outage
  count, status — is **outside** the identity payload, so finishing or
  resuming a run never changes its name.
- `<nonce4>` is four hex digits from `os.urandom`, and the directory is
  allocated atomically with `mkdir` (exclusive); on collision a new nonce is
  drawn. Two identical runs started in the same second therefore get distinct
  directories, and nothing is ever overwritten.
- The runner writes the manifest with `status: "incomplete"` **before the
  first case**, appends each case's terminal row to `cases.csv` atomically
  (write to a temp file, rename), and so can be interrupted at any point. On
  `--resume` it keeps the run ID, re-reads the saved manifest, **refuses**
  unless the current identity payload equals the saved one (so retried cases
  are never combined with results from different code, prompt, suite or
  settings), validates that the saved rows' IDs are a subset of the selected
  ID set, keeps every saved terminal row, and runs the cases that are
  **unattempted or `outage`**. A run becomes `complete` only when every
  selected case has a terminal outcome and none is `outage`; a partial run
  can never present a partial denominator as complete. A checksum of the
  final manifest is recorded as `manifest_sha256`, computed with that field
  blank, separate from the identity hash.

The directory holds three files:

- `manifest.json`: repo commit **and whether the tree was dirty** (a dirty
  run is marked `citable: false`), suite name and the **SHA-256 of the
  normalised suite file**, subset name and the **SHA-256 of the ID list** (or
  `full`), a **`source` block copied verbatim from the suite's `.source.json`**
  — for a public suite `kind: "download"`, `release`, `url`, `sha256`,
  `licence`; for an authored suite `kind: "authored"`, `author`, `licence`
  (the repository's) — **adapter version**,
  scorer version (a constant in `evaluation.py` bumped on any scoring
  change), prompt hash (SHA-256 of the assembled system prompt for the
  engine), provider, model, `use_rag`, `rag_top_k`, `evidence`, `work_limit`,
  `max_rows`, `max_repair_attempts`, **retry policy and the outage count**,
  start time, duration, Python and package versions, and a `status` of
  `complete` or `incomplete`.
- `cases.csv`: one row per case — outcome, generated SQL, error code
  (DSN-redacted), latency, tokens.
- `report.md`: the metrics with intervals, overall and per hardness, and the
  manifest's identifying fields as a header.

Rules: a result is never cited without its manifest; only a `complete`,
`citable` run may be cited; the README's tables carry date and commit and link
to the directory; `report.md` states that v2 numbers are not comparable to the
May tables; the old files stay where they are, labelled historical.

**Outage policy.** A provider error that survives the retry policy (429, 5xx,
timeout) marks the case `outage`, not `error`. A run with any `outage` is
`incomplete`: its report is written, with the count, but it is not citable and
not eligible for the regression gate until re-run with `--resume`, which
retries only the `outage` cases. Outages are therefore never counted as model
failures and never silently dropped.

The gold baseline keeps its role as a gate, defined **separately from
headline EX**. `--mode gold` runs no model; the gate passes when all three
hold: every *valid* reference executes and self-matches under `rows_equal_v2`;
every `reference_invalid` ID is on the suite's reviewed exception list; and
every non-answerable case is well-formed — no gold SQL, and an `expected`
whose correct code is one the pipeline can produce. Headline EX and reference
coverage are still reported for the gold run, and a suite with excepted
references passes the gate while showing headline EX below 100 % — nine valid
self-matching references plus one excepted invalid one is a passing gate at
90 %. Two populations must not be confused: a suite whose answerable cases
**all** have invalid references reports headline EX `0 / N = 0 %` and
conditional EX `0 / 0 (undefined)`, and fails the gate unless every one is
excepted; a suite with **no answerable cases at all** (the `safety` suite is
one by design) has no EX to report — both are marked *not applicable* — and
passes the gate on its structural checks alone. The zero-valid-reference
failure applies only when answerable cases exist. Safety *accuracy* is a
model metric and is reported only for model runs; gold mode never claims it.

### 4.5 Run policy and the CI gate

- **Routine:** `spider_dev.subset200`, `bird_dev.subset200` twice (evidence on
  and off), `safety` and `demo` in full — about 600 LLM calls.
- **Release:** the full dev sets, once per version, producing the dated
  results the README cites.
- **CI gate (every push, no provider, no network):** `--mode gold` over every
  suite whose data is present. `safety` and `demo` always run; the public
  subsets run only when the archives are cached on the runner and otherwise
  skip with an explicit reason, the same pattern as the PostgreSQL tests. The
  gold gate as defined in §4.4 must pass: valid references self-match, invalid
  ones are on the exception list, non-answerable cases are well-formed. This
  proves the harness, not the model.
- **Accuracy regression gate (on demand, needs a model):** compares a new
  `complete` run with a named baseline run. The two are **compatible** only if
  their manifests agree on suite hash, subset hash, source release, scorer
  version, adapter version, provider, model, `evidence`, `use_rag`,
  `rag_top_k`, `work_limit`, `max_rows`, `max_repair_attempts` and retry
  policy — so the only things allowed to differ are the commit and the prompt
  hash, which is what the gate exists to test. An incompatible or incomplete
  pair is rejected with the differing fields named, never compared.

  The decision rule is **paired**, because the cases are the same: for each
  case, `new − old` on the correct indicator; a paired bootstrap (10,000
  resamples, fixed seed) gives a 95 % interval for the mean difference. The
  gate **fails** if that interval lies entirely below zero, **or** if the point
  drop is 5 percentage points or more regardless of the interval (a practical
  floor that also covers the degenerate 0/n and n/n cases, where a one-run
  interval has zero width). The per-run case bootstrap in §4.3 describes
  sampling uncertainty over cases; repeated local runs, as the direction asks
  for on the ablation, describe generation variance, and the report states
  which of the two an interval is. Definition of done includes observing the
  gate fail on a deliberately broken prompt, pass on the fix, and reject an
  incompatible pair.

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
- Comparator: the four reproduced lenient cases (`'A'` vs `'a'`, `NULL` vs
  `'None'`, `10.004` vs `10.0`, `[2, 1]` vs `[1, 2]` under an `ORDER BY`) are
  each **not** equal under v2 and still equal under the legacy comparator;
  `1` vs `1.0` and `0.1 + 0.2` vs `0.3` are equal; a duplicated row is not;
  unordered equality holds without an `ORDER BY`.
- Blocked gold: a fixture case whose gold SQL the validator refuses scores
  `reference_invalid` without being executed, stays in the headline
  denominator, is excluded from the conditional score, is listed by ID, fails
  the gold gate, and passes it once its ID is on the exception list — with the
  gate passing while headline EX reads 9/10. Three population fixtures: a
  safety-only suite passes on structural checks with EX not applicable; an
  all-invalid answerable suite reports `0 / N` headline and `0 / 0
  (undefined)` conditional and fails; a mixed suite reports both populations
  correctly.
- Comparator matching: the pair `gold = [(1.0, "b"), (1.0000001, "a")]`,
  `generated = [(1.0, "a"), (1.0000001, "b")]` is equal unordered (a sort by
  repr would say otherwise); an ambiguous fixture where a greedy first-match
  fails but a perfect matching exists is equal; the extra-duplicate case is
  rejected by both v1 and v2.
- Crash resume: interrupt a run after one successful case; resume keeps that
  row, runs the remaining cases exactly once, and the run completes with the
  same directory.
- Provenance: a runner fixture with distinctive `sha256` and `licence` values
  in its `.source.json` round-trips them into the manifest; an authored suite
  yields `kind: "authored"`.
- Run identity: evidence on/off, RAG on/off and two identical runs with a
  frozen timestamp produce distinct directories (the nonce differs, the
  identity hash does not); the identity hash is unchanged by status,
  duration and outage count; a model name containing `/` and `:` yields a
  valid path; a dirty tree is recorded and marks the run not citable; an
  outage-to-complete resume keeps the same directory and refuses when the
  identity payload differs from the saved manifest.
- Outage: a persistent provider error yields `outage`, the run is
  `incomplete`, and `--resume` retries only those cases.
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
- Regression gate: fails on a synthetic paired set whose difference interval
  lies below zero, fails on a 5-point drop with an interval spanning zero,
  passes on a small drop, rejects a pair differing in subset hash or scorer
  version with the fields named, and rejects an incomplete run.
- The May result files are untouched: a test asserts their contents' hashes.

## 5. Risks

- **Benchmark downloads move or change.** The adapters record the archive
  hash, and a hash mismatch is a hard failure with the recorded value in the
  message, so a silently different release cannot produce comparable-looking
  numbers.
- **BIRD gold SQL that our validator refuses.** Expected for a handful of
  cases; scored `reference_invalid`, kept in the headline denominator, listed
  by ID, and gated by the exception list (§4.2–4.4). Each one is also a free
  probe of the validator and is logged for review.
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
