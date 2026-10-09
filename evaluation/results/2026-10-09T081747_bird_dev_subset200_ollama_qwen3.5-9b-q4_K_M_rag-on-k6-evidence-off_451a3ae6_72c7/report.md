# Evaluation v2: bird_dev / subset200 / ollama / qwen3.5:9b-q4_K_M

Status: **complete** - citable: **yes** - 200 of 200 cases recorded - 0 outage(s).

## Identity

| Field | Value |
|---|---|
| run_id | `2026-10-09T081747_bird_dev_subset200_ollama_qwen3.5-9b-q4_K_M_rag-on-k6-evidence-off_451a3ae6_72c7` |
| identity_sha256 | `451a3ae66143ed0618a2a2d1533dbcfd13f6a261f51a56552c41c59cd1b91c72` |
| commit | `7c1aaa4749b19da8a12ff4b5da0eb665a0fe2672` |
| dirty | `False` |
| suite | `bird_dev` |
| suite_sha256 | `e774beb1020e7c8ee420742ae26e36e74e23fe4706a4a09ede225ead12101bde` |
| subset | `subset200` |
| subset_sha256 | `bcf73ad96c8c2f9a08dca680e5b4bc36bf062551c7ddbcb6fe40841a27296361` |
| source_release | `bird-sql-dev-20251106 (2025-11-13 development split; questions birdsql/bird_sql_dev_20251106@3c11fb193e54, databases dev.zip dev_20240627)` |
| database_fingerprint | `(('data/benchmarks/bird/dev_databases/california_schools/california_schools.sqlite', '986817d793479801ed55133e55aa27e335422c0cd3866b54a3d6317b7c5f09c1'), ('data/benchmarks/bird/dev_databases/card_games/card_games.sqlite', 'c98bdb57fe7474da798b407785544b9af0daaad5d61fd21e2a73309493bc1227'), ('data/benchmarks/bird/dev_databases/codebase_community/codebase_community.sqlite', '92101be6d2a9f6adceea59d38f6d1c556087f9eea1432432a21268cb1348036d'), ('data/benchmarks/bird/dev_databases/debit_card_specializing/debit_card_specializing.sqlite', 'b3d149ad05746dbbe5116e229e17e18f09c39db43cf117d9ef3441753608b691'), ('data/benchmarks/bird/dev_databases/european_football_2/european_football_2.sqlite', 'e4d361dbeec6591a4b315877c0c481da05c8ff5a3d389b34d6e17b6f15bee4e0'), ('data/benchmarks/bird/dev_databases/financial/financial.sqlite', 'd15d89cdb068a202b6f2b99342af44dffc1d52545b39ceaf62efdc0ba570101e'), ('data/benchmarks/bird/dev_databases/formula_1/formula_1.sqlite', '17185981cd747f6cdc374cb02a6096db3130e6ec2ddc582fe1686a28fb4c4c8a'), ('data/benchmarks/bird/dev_databases/student_club/student_club.sqlite', 'eb89bcfe97eefa386a27904ec5aa15159811a7eac894ec659a36e48fa9f76b77'), ('data/benchmarks/bird/dev_databases/superhero/superhero.sqlite', '75e94a2c3236ee3bb2c01fb97a1c4b4c1c269bcefd4eab1d04be323d2d0825b1'), ('data/benchmarks/bird/dev_databases/thrombosis_prediction/thrombosis_prediction.sqlite', 'e7e16d74b4731b4b8d33fdbe8c29cd5622620788ce8d1f335631f65fc7cf1db9'), ('data/benchmarks/bird/dev_databases/toxicology/toxicology.sqlite', 'f5fa7f21af1ad878ff8fef1b0582b8cb2d7ed63dbac65ff16d2ba05667650c5b'))` |
| adapter_version | `3` |
| scorer_version | `2` |
| prompt_sha256 | `89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3` |
| provider | `ollama` |
| model | `qwen3.5:9b-q4_K_M` |
| evidence | `False` |
| use_rag | `True` |
| rag_top_k | `6` |
| work_limit | `1000000000` |
| max_rows | `100000` |
| max_repair_attempts | `1` |
| retry_policy | `2x10.0` |
| mode | `llm` |
| source | kind=download, release=bird-sql-dev-20251106 (2025-11-13 development split; questions birdsql/bird_sql_dev_20251106@3c11fb193e54, databases dev.zip dev_20240627), url=https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip, sha256=cdd6d19faeb45a23970b98d3ef6c40a87987c95459c2cf12076897a60cf5a630, licence=cc-by-sa-4.0, adapter_version=3, unpacked_at=2026-10-09T04:23:58+00:00, questions_url=https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106/resolve/3c11fb193e5439b338e23677fa0aae11e8b85db9/data/dev_20251106-00000-of-00001.json, questions_sha256=ffd8018378ddb1a8794753e0a31cfc81862ff7318a5184c22f3dc4ce03a03feb, licence_source=front matter of birdsql/bird_sql_dev_20251106@3c11fb193e54 README.md (dev.zip bundles no licence file; bird-bench.github.io agrees: CC BY-SA 4.0 since 2024-04-27), licence_source_sha256=2ed0fcf8e873ef953d70bcf5e96042155b86a534d7b7c550509f7903dbfaeed5 |
| started | `2026-10-09T08:17:47+00:00` |
| duration_s | `5365.14` |
| outage_count | `0` |
| generation_failures | `0` |

## Metrics

| Population | Cases | EX (headline) | EX over valid references | Reference coverage | Safety accuracy | Declined as unanswerable (refusal cases) | False-refusal rate | Schema recall (mean of per-case fractions) |
|---|---:|---|---|---|---|---|---|---|
| overall | 200 | 17.0% [12.0, 22.5] (34/200) | 17.2% [12.1, 22.7] (34/198) | 99.0% [97.5, 100.0] (198/200) | not applicable | not applicable | 4.5% [2.0, 7.5] (9/200) | 99.6% [99.1, 100.0] (n=200) |
| easy | 112 | 26.8% [18.8, 34.8] (30/112) | 27.0% [18.9, 35.1] (30/111) | 99.1% [97.3, 100.0] (111/112) | not applicable | not applicable | 3.6% [0.9, 7.1] (4/112) | 100.0% [100.0, 100.0] (n=112) |
| medium | 58 | 6.9% [1.7, 13.8] (4/58) | 6.9% [1.7, 13.8] (4/58) | 100.0% [100.0, 100.0] (58/58) | not applicable | not applicable | 8.6% [1.7, 17.2] (5/58) | 100.0% [100.0, 100.0] (n=58) |
| hard | 30 | 0.0% [0.0, 0.0] (0/30) | 0.0% [0.0, 0.0] (0/29) | 96.7% [90.0, 100.0] (29/30) | not applicable | not applicable | 0.0% [0.0, 0.0] (0/30) | 97.4% [94.0, 100.0] (n=30) |

Each rate cell is `point [low, high] (k/n)` in percent: a 95 % percentile-bootstrap interval, 10,000 resamples over cases, seed 0. Schema recall is not a rate: its cell is `mean [low, high] (n=cases)`, the same bootstrap over the mean of per-case recall fractions. What an interval means: it is sampling uncertainty over cases, for a fixed model and prompt. It is not generation variance across repeated runs of the same model.

EX (headline) counts every answerable case and a `reference_invalid` one as not correct; EX over valid references excludes those; reference coverage is valid references over all answerable cases. Safety accuracy is over refusal and unanswerable cases only. Declined as unanswerable is over refusal cases only: the model answered the sentinel instead of refusing, which is safe but not counted in safety accuracy.

These scorer v2 numbers are not comparable to the May 2026 12-case tables (`evaluation/results/evaluation_llm_*`). v2 applies a different comparison policy - typed values, exact text, multiset rows, order only under a top-level `ORDER BY` - not a uniformly stricter version of v1's, so a difference between the two is neither an improvement nor a regression.

## Outcomes

| Outcome | Cases |
|---|---:|
| correct | 34 |
| error | 114 |
| reference_invalid | 2 |
| refused | 9 |
| unanswerable | 10 |
| wrong | 31 |

## Reference-invalid cases

- `701`
- `1131`

## Generation failures

0 of 200 terminal case(s) failed before the model answered - generation raised a non-retryable provider error, or the harness did - and are scored `error` (their IDs are the rows with `generation_failure` set in `cases.csv`). A key that dies partway through a run shows here even when the run is citable.

## Latency and tokens

- Latency: median 25591.8 ms, mean 26281.3 ms over 200 cases.
- Tokens: not reported by this provider interface (blank in `cases.csv`).
