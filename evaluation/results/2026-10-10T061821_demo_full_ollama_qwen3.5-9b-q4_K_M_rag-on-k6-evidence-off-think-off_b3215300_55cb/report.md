# Evaluation v2: demo / full / ollama / qwen3.5:9b-q4_K_M

Status: **complete** - citable: **yes** - 12 of 12 cases recorded - 0 outage(s).

## Identity

| Field | Value |
|---|---|
| run_id | `2026-10-10T061821_demo_full_ollama_qwen3.5-9b-q4_K_M_rag-on-k6-evidence-off-think-off_b3215300_55cb` |
| identity_sha256 | `b321530016d53710317586c524c6985b58470ee170b5ea7284acbaf9962f9563` |
| commit | `77082957a8d6306fde5efc8e00f7f5f6d6c65c5a` |
| dirty | `False` |
| suite | `demo` |
| suite_sha256 | `3b92ec3016801f53d324d60003fd71f020cf230f92b542cee669d38c004b629d` |
| subset | `full` |
| subset_sha256 | `` |
| source_release | `n/a` |
| database_fingerprint | `(('data/healthcare_analytics.db', '87fb173637a5e294dacdf3b889e9c4b362732f54a9aae304f4365731e3c22cc2'), ('data/retail_analytics.db', 'a7da556d5d23044ca55a0b3147684ad671ba2172a358452017dab215d5713275'), ('data/university_agent.db', 'e4e9400e104e81c709421fc6c5e4de956f8a4317d7d9387f4cb1a74eaec0078b'))` |
| adapter_version | `n/a` |
| scorer_version | `2` |
| prompt_sha256 | `89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3` |
| provider | `ollama` |
| model | `qwen3.5:9b-q4_K_M` |
| evidence | `False` |
| use_rag | `True` |
| rag_top_k | `6` |
| work_limit | `100000` |
| max_rows | `1000` |
| max_repair_attempts | `1` |
| retry_policy | `2x10.0` |
| ollama_think | `off` |
| mode | `llm` |
| source | kind=authored, author=repository, licence=MIT, adapter_version=n/a |
| started | `2026-10-10T06:18:21+00:00` |
| duration_s | `49.466` |
| outage_count | `0` |
| generation_failures | `0` |

## Metrics

| Population | Cases | EX (headline) | EX over valid references | Reference coverage | Safety accuracy | Declined as unanswerable (refusal cases) | False-refusal rate | Schema recall (mean of per-case fractions) |
|---|---:|---|---|---|---|---|---|---|
| overall | 12 | 33.3% [8.3, 58.3] (4/12) | 33.3% [8.3, 58.3] (4/12) | 100.0% [100.0, 100.0] (12/12) | not applicable | not applicable | 0.0% [0.0, 0.0] (0/12) | 100.0% [100.0, 100.0] (n=12) |
| easy | 5 | 60.0% [20.0, 100.0] (3/5) | 60.0% [20.0, 100.0] (3/5) | 100.0% [100.0, 100.0] (5/5) | not applicable | not applicable | 0.0% [0.0, 0.0] (0/5) | 100.0% [100.0, 100.0] (n=5) |
| medium | 4 | 25.0% [0.0, 75.0] (1/4) | 25.0% [0.0, 75.0] (1/4) | 100.0% [100.0, 100.0] (4/4) | not applicable | not applicable | 0.0% [0.0, 0.0] (0/4) | 100.0% [100.0, 100.0] (n=4) |
| hard | 3 | 0.0% [0.0, 0.0] (0/3) | 0.0% [0.0, 0.0] (0/3) | 100.0% [100.0, 100.0] (3/3) | not applicable | not applicable | 0.0% [0.0, 0.0] (0/3) | 100.0% [100.0, 100.0] (n=3) |

Each rate cell is `point [low, high] (k/n)` in percent: a 95 % percentile-bootstrap interval, 10,000 resamples over cases, seed 0. Schema recall is not a rate: its cell is `mean [low, high] (n=cases)`, the same bootstrap over the mean of per-case recall fractions. What an interval means: it is sampling uncertainty over cases, for a fixed model and prompt. It is not generation variance across repeated runs of the same model.

EX (headline) counts every answerable case and a `reference_invalid` one as not correct; EX over valid references excludes those; reference coverage is valid references over all answerable cases. Safety accuracy is over refusal and unanswerable cases only. Declined as unanswerable is over refusal cases only: the model answered the sentinel instead of refusing, which is safe but not counted in safety accuracy.

These scorer v2 numbers are not comparable to the May 2026 12-case tables (`evaluation/results/evaluation_llm_*`). v2 applies a different comparison policy - typed values, exact text, multiset rows, order only under a top-level `ORDER BY` - not a uniformly stricter version of v1's, so a difference between the two is neither an improvement nor a regression.

## Outcomes

| Outcome | Cases |
|---|---:|
| correct | 4 |
| wrong | 8 |

## Reference-invalid cases

None.

## Generation failures

0 of 12 terminal case(s) failed before the model answered - generation raised a non-retryable provider error, or the harness did - and are scored `error` (their IDs are the rows with `generation_failure` set in `cases.csv`). A key that dies partway through a run shows here even when the run is citable.

## Latency and tokens

- Latency: median 3331.9 ms, mean 4114.0 ms over 12 cases.
- Tokens, prompt: 19496 total, 1624.7 mean over 12 cases that reported them.
- Tokens, completion: 570 total, 47.5 mean over 12 cases that reported them.
