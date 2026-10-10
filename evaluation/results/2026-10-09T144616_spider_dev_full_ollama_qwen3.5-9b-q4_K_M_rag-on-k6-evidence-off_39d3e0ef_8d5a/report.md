# Evaluation v2: spider_dev / full / ollama / qwen3.5:9b-q4_K_M

Status: **complete** - citable: **yes** - 1034 of 1034 cases recorded - 0 outage(s).

## Identity

| Field | Value |
|---|---|
| run_id | `2026-10-09T144616_spider_dev_full_ollama_qwen3.5-9b-q4_K_M_rag-on-k6-evidence-off_39d3e0ef_8d5a` |
| identity_sha256 | `39d3e0efa7c20241cc3f50fddc90db31147e414c087c10822872e3034498af98` |
| commit | `a8b506972b18062a5679e74558d297e780e4b11c` |
| dirty | `False` |
| suite | `spider_dev` |
| suite_sha256 | `04f185608f946e75e2a37a65b078b5925798de8e4b9950631acebaa2a6ae0091` |
| subset | `full` |
| subset_sha256 | `` |
| source_release | `spider-1.0 dev (spider_data.zip, dev.json revised 2020-08-03)` |
| database_fingerprint | `(('data/benchmarks/spider/database/battle_death/battle_death.sqlite', '12569f4493a9655639c4ea86ba4bd8a4ea6411e1b8ae0e1fdf3d6a995344265d'), ('data/benchmarks/spider/database/car_1/car_1.sqlite', '9d851e396e02997a1de073ae982fe1e4b1769fdffce2fac6e325857a3a938709'), ('data/benchmarks/spider/database/concert_singer/concert_singer.sqlite', '4fa1ba5ab4577e895271088b1dc44aa94be88e25a54293317a67584112ef059d'), ('data/benchmarks/spider/database/course_teach/course_teach.sqlite', 'da45fcdde64ac2b9330146506b7653ad4417489d8e496e509045e1b02245d793'), ('data/benchmarks/spider/database/cre_Doc_Template_Mgt/cre_Doc_Template_Mgt.sqlite', '9c3fdd03d8795ecde60aae782ce50c4fb1d6c03de1401fcdd6dc177342a53df5'), ('data/benchmarks/spider/database/dog_kennels/dog_kennels.sqlite', 'ea5fbc6cf6aaf500371c5c121c339d9802ba6f5fbecd948a5274c0b487a9f486'), ('data/benchmarks/spider/database/employee_hire_evaluation/employee_hire_evaluation.sqlite', '760fec5cc872ef0cdf99242614f05c93dbd00d422ac9da87ea64fcf555be7373'), ('data/benchmarks/spider/database/flight_2/flight_2.sqlite', 'db777ba6488ae2b515cb138d71ff23c4533e5fd0355854648a1102faff3fb7af'), ('data/benchmarks/spider/database/museum_visit/museum_visit.sqlite', '3217aee38d8e3661d374bf025591b7164a1179c568ef5d20f3c338035fc6e1dd'), ('data/benchmarks/spider/database/network_1/network_1.sqlite', 'bec92cd9a68d6f23469a0df246429299f44caaf75eab12275784e0bb7d0aa9ca'), ('data/benchmarks/spider/database/orchestra/orchestra.sqlite', '13b64af1cf6fcbd34953a44f951b2bf79bedc000b344549212dac1a9345e2f86'), ('data/benchmarks/spider/database/pets_1/pets_1.sqlite', '8ffe0ea9b3b034ca8860a2f18300a98c7583ce2522ea195726a04803f675e5bb'), ('data/benchmarks/spider/database/poker_player/poker_player.sqlite', '63fffd7b68524f07a742a74b74b95a4ba178e6419f3574b9ee78b8dda1df46ba'), ('data/benchmarks/spider/database/real_estate_properties/real_estate_properties.sqlite', '5d0bd83afa93a843e68f892d8430fe625d7ed2508aae00a341926b0b5b478a29'), ('data/benchmarks/spider/database/singer/singer.sqlite', '5c1b72755d148c294b26d7b479307e8799b93485ac918720f953e2ab84d59a0a'), ('data/benchmarks/spider/database/student_transcripts_tracking/student_transcripts_tracking.sqlite', '5a1f4928a1fd36f6edb31c4f054203b2e2fab6b18e5951c4f5633628f82a8e56'), ('data/benchmarks/spider/database/tvshow/tvshow.sqlite', '1212c2e75cf1dcf3aeb4055331a85298b073d8a613ead77975b13bd3346b9fc4'), ('data/benchmarks/spider/database/voter_1/voter_1.sqlite', '815bf1611b8a32f649d292a6138562f1761a0c10cb256e20f6561199cff31531'), ('data/benchmarks/spider/database/world_1/world_1.sqlite', '17b986695f16786d58d66f85e49dba87bdfe72953207ab9b1b49da9d2301ef65'), ('data/benchmarks/spider/database/wta_1/wta_1.sqlite', '8f20747456d7748674b4f05432f3de96446ced95b8bd5a6fd01284f6c28f727c'))` |
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
| source | kind=download, release=spider-1.0 dev (spider_data.zip, dev.json revised 2020-08-03), url=https://drive.usercontent.google.com/download?id=1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J&export=download&confirm=t, sha256=00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b, licence=CC BY-SA 4.0, adapter_version=3, unpacked_at=2026-10-09T04:23:56+00:00, licence_source=https://yale-lily.github.io/spider (the archive bundles no licence file) |
| started | `2026-10-09T14:46:16+00:00` |
| duration_s | `20104.314` |
| outage_count | `0` |
| generation_failures | `0` |

## Metrics

| Population | Cases | EX (headline) | EX over valid references | Reference coverage | Safety accuracy | Declined as unanswerable (refusal cases) | False-refusal rate | Schema recall (mean of per-case fractions) |
|---|---:|---|---|---|---|---|---|---|
| overall | 1034 | 56.1% [53.1, 59.2] (580/1034) | 56.2% [53.2, 59.2] (580/1032) | 99.8% [99.5, 100.0] (1032/1034) | not applicable | not applicable | 2.6% [1.6, 3.6] (27/1034) | 100.0% [100.0, 100.0] (n=1034) |
| easy | 248 | 79.8% [74.6, 84.7] (198/248) | 79.8% [74.6, 84.7] (198/248) | 100.0% [100.0, 100.0] (248/248) | not applicable | not applicable | 2.0% [0.4, 4.0] (5/248) | 100.0% [100.0, 100.0] (n=248) |
| medium | 446 | 64.1% [59.6, 68.4] (286/446) | 64.4% [59.9, 68.9] (286/444) | 99.6% [98.9, 100.0] (444/446) | not applicable | not applicable | 1.1% [0.2, 2.2] (5/446) | 100.0% [100.0, 100.0] (n=446) |
| hard | 174 | 34.5% [27.6, 41.4] (60/174) | 34.5% [27.6, 41.4] (60/174) | 100.0% [100.0, 100.0] (174/174) | not applicable | not applicable | 5.2% [2.3, 8.6] (9/174) | 100.0% [100.0, 100.0] (n=174) |
| extra | 166 | 21.7% [15.7, 28.3] (36/166) | 21.7% [15.7, 28.3] (36/166) | 100.0% [100.0, 100.0] (166/166) | not applicable | not applicable | 4.8% [1.8, 8.4] (8/166) | 100.0% [100.0, 100.0] (n=166) |

Each rate cell is `point [low, high] (k/n)` in percent: a 95 % percentile-bootstrap interval, 10,000 resamples over cases, seed 0. Schema recall is not a rate: its cell is `mean [low, high] (n=cases)`, the same bootstrap over the mean of per-case recall fractions. What an interval means: it is sampling uncertainty over cases, for a fixed model and prompt. It is not generation variance across repeated runs of the same model.

EX (headline) counts every answerable case and a `reference_invalid` one as not correct; EX over valid references excludes those; reference coverage is valid references over all answerable cases. Safety accuracy is over refusal and unanswerable cases only. Declined as unanswerable is over refusal cases only: the model answered the sentinel instead of refusing, which is safe but not counted in safety accuracy.

These scorer v2 numbers are not comparable to the May 2026 12-case tables (`evaluation/results/evaluation_llm_*`). v2 applies a different comparison policy - typed values, exact text, multiset rows, order only under a top-level `ORDER BY` - not a uniformly stricter version of v1's, so a difference between the two is neither an improvement nor a regression.

## Outcomes

| Outcome | Cases |
|---|---:|
| correct | 580 |
| error | 228 |
| reference_invalid | 2 |
| refused | 27 |
| wrong | 197 |

## Reference-invalid cases

- `455`
- `456`

## Generation failures

0 of 1034 terminal case(s) failed before the model answered - generation raised a non-retryable provider error, or the harness did - and are scored `error` (their IDs are the rows with `generation_failure` set in `cases.csv`). A key that dies partway through a run shows here even when the run is citable.

## Latency and tokens

- Latency: median 18924.7 ms, mean 19429.8 ms over 1034 cases.
- Tokens, prompt: 1478932 total, 1430.3 mean over 1034 cases that reported them.
- Tokens, completion: 402115 total, 388.9 mean over 1034 cases that reported them.
