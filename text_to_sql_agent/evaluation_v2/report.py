"""``report.md``: the identity header and the metrics with intervals (spec §4.3-§4.4).

Metrics are computed from the rows exactly as ``cases.csv`` stores them (every value a
string), so a saved run can be re-rendered from its own files without re-running anything.

Denominators (spec §4.3), one policy everywhere:

- **EX (headline)** = ``correct`` / all answerable cases; ``reference_invalid`` counts as not
  correct, so a validator change that blocks more gold queries can only lower it.
- **EX over valid references** = ``correct`` / answerable cases whose reference is valid;
  ``0/0 (undefined)`` when every reference is invalid.
- **Reference coverage** = valid references / all answerable cases.
- **Safety accuracy** = ``correct`` / refusal and unanswerable cases. A model metric: a gold
  run never claims it.
- **False-refusal rate** = answerable cases scored ``refused`` / all answerable cases. Also a
  model metric (the cost of default-deny), so a gold run never claims it either.
- **Schema recall** = mean per-case recall of ``expected_tables`` among retrieved tables.

A suite with no answerable cases has no EX at all: every EX cell is *not applicable*.
``outage`` rows are not terminal outcomes and sit outside every denominator.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from text_to_sql_agent.evaluation_v2.identity import IDENTITY_FIELDS
from text_to_sql_agent.evaluation_v2.manifest import Manifest, write_atomic
from text_to_sql_agent.evaluation_v2.stats import Interval, bootstrap_ci, bootstrap_mean_ci

Row = dict[str, str]

RESAMPLES = 10_000
SEED = 0
HARDNESS_ORDER = ("easy", "medium", "hard", "extra")

# Copied, not paraphrased, from `stats.py`'s module docstring; a test holds them equal.
INTERVAL_MEANING = (
    "What an interval means: it is sampling uncertainty over cases, for a fixed model and "
    "prompt. It is not generation variance across repeated runs of the same model."
)
NOT_COMPARABLE = (
    "These scorer v2 numbers are not comparable to the May 2026 12-case tables "
    "(`evaluation/results/evaluation_llm_*`). v2 applies a different comparison policy - "
    "typed values, exact text, multiset rows, order only under a top-level `ORDER BY` - not a "
    "uniformly stricter version of v1's, so a difference between the two is neither an "
    "improvement nor a regression."
)

NOT_APPLICABLE = "not applicable"
NOT_APPLICABLE_GOLD = "not applicable (gold run)"
UNDEFINED_EMPTY = "0/0 (undefined)"

COLUMNS: tuple[tuple[str, str], ...] = (
    ("ex", "EX (headline)"),
    ("ex_valid", "EX over valid references"),
    ("coverage", "Reference coverage"),
    ("safety", "Safety accuracy"),
    ("false_refusal", "False-refusal rate"),
    ("recall", "Schema recall"),
)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}"


def _interval_text(interval: Interval) -> str:
    return f"{_pct(interval.point)}% [{_pct(interval.low)}, {_pct(interval.high)}]"


def _rate(flags: Sequence[bool]) -> str:
    interval = bootstrap_ci(flags, resamples=RESAMPLES, seed=SEED)
    return f"{_interval_text(interval)} ({sum(flags)}/{len(flags)})"


def terminal(rows: Sequence[Row]) -> list[Row]:
    """Rows with a terminal outcome: everything except ``outage``."""
    return [row for row in rows if row["outcome"] != "outage"]


def metric_cells(rows: Sequence[Row], *, gold: bool) -> dict[str, str]:
    """One population's metric cells, keyed as ``COLUMNS``, each ``point [low, high] (k/n)``."""
    done = terminal(rows)
    answerable = [r for r in done if r["expected"] == "answerable"]
    others = [r for r in done if r["expected"] != "answerable"]
    valid = [r for r in answerable if r["outcome"] != "reference_invalid"]
    recalls = [float(r["schema_recall"]) for r in done if r.get("schema_recall", "")]

    cells: dict[str, str] = {}
    if answerable:
        cells["ex"] = _rate([r["outcome"] == "correct" for r in answerable])
        cells["ex_valid"] = (
            _rate([r["outcome"] == "correct" for r in valid]) if valid else UNDEFINED_EMPTY
        )
        cells["coverage"] = _rate([r["outcome"] != "reference_invalid" for r in answerable])
    else:
        cells["ex"] = cells["ex_valid"] = cells["coverage"] = NOT_APPLICABLE
    if gold:
        cells["safety"] = cells["false_refusal"] = NOT_APPLICABLE_GOLD
    else:
        cells["safety"] = (
            _rate([r["outcome"] == "correct" for r in others]) if others else NOT_APPLICABLE
        )
        cells["false_refusal"] = (
            _rate([r["outcome"] == "refused" for r in answerable]) if answerable else NOT_APPLICABLE
        )
    if recalls:
        interval = bootstrap_mean_ci(recalls, resamples=RESAMPLES, seed=SEED)
        cells["recall"] = f"{_interval_text(interval)} (n={len(recalls)})"
    else:
        cells["recall"] = NOT_APPLICABLE
    return cells


def _metrics_table(rows: Sequence[Row], *, gold: bool) -> list[str]:
    header = "| Population | Cases | " + " | ".join(title for _, title in COLUMNS) + " |"
    lines = [header, "|---|---:|" + "---|" * len(COLUMNS)]
    populations: list[tuple[str, list[Row]]] = [("overall", list(rows))]
    for hardness in HARDNESS_ORDER:
        subset = [r for r in rows if r["hardness"] == hardness]
        if subset:
            populations.append((hardness, subset))
    for name, population in populations:
        cells = metric_cells(population, gold=gold)
        values = " | ".join(cells[key] for key, _ in COLUMNS)
        lines.append(f"| {name} | {len(terminal(population))} | {values} |")
    return lines


def _ids(rows: Sequence[Row], outcome: str) -> list[str]:
    return [row["id"] for row in rows if row["outcome"] == outcome]


def _latency_and_tokens(rows: Sequence[Row]) -> list[str]:
    latencies = [float(r["latency_ms"]) for r in terminal(rows) if r.get("latency_ms", "")]
    lines: list[str] = []
    if latencies:
        lines.append(
            f"- Latency: median {statistics.median(latencies):.1f} ms, mean "
            f"{statistics.fmean(latencies):.1f} ms over {len(latencies)} cases."
        )
    prompt = [int(r["prompt_tokens"]) for r in rows if r.get("prompt_tokens", "")]
    completion = [int(r["completion_tokens"]) for r in rows if r.get("completion_tokens", "")]
    if prompt or completion:
        lines.append(
            f"- Tokens: {sum(prompt)} prompt, {sum(completion)} completion "
            f"(over the {max(len(prompt), len(completion))} cases that reported them)."
        )
    else:
        lines.append("- Tokens: not reported by this provider interface (blank in `cases.csv`).")
    return lines


def render_report(manifest: Manifest, rows: Sequence[Row]) -> str:
    """The full ``report.md`` text for a run's manifest and its ``cases.csv`` rows."""
    data = manifest.to_dict()
    identity = manifest.identity
    gold = manifest.mode == "gold"
    status_line = (
        f"Status: **{manifest.status}** - citable: **{'yes' if manifest.citable else 'no'}** - "
        f"{len(rows)} of {manifest.case_count} cases recorded - "
        f"{manifest.outage_count} outage(s)."
    )
    lines = [
        f"# Evaluation v2: {identity.suite} / {identity.subset} / "
        f"{identity.provider} / {identity.model}",
        "",
        status_line,
        "",
    ]
    reasons: list[str] = []
    if identity.dirty:
        reasons.append("the working tree was dirty, so the commit does not pin the code")
    if manifest.status != "complete":
        reasons.append(
            "the run is incomplete - resume it with `--resume` on this directory, which runs "
            "only the unattempted and `outage` cases"
        )
    if reasons:
        lines += [f"**Not citable:** {'; '.join(reasons)}.", ""]

    lines += ["## Identity", "", "| Field | Value |", "|---|---|"]
    lines.append(f"| run_id | `{manifest.run_id}` |")
    lines.append(f"| identity_sha256 | `{data['identity_sha256']}` |")
    for name in IDENTITY_FIELDS:
        lines.append(f"| {name} | `{data[name]}` |")
    source = ", ".join(f"{key}={value}" for key, value in manifest.source.items())
    lines += [
        f"| mode | `{manifest.mode}` |",
        f"| source | {source} |",
        f"| started | `{manifest.started}` |",
        f"| duration_s | `{manifest.duration_s}` |",
        f"| outage_count | `{manifest.outage_count}` |",
        "",
        "## Metrics",
        "",
        *_metrics_table(rows, gold=gold),
        "",
        "Each cell is `point [low, high] (k/n)` in percent: a 95 % percentile-bootstrap "
        f"interval, {RESAMPLES:,} resamples over cases, seed {SEED}. {INTERVAL_MEANING}",
        "",
        "EX (headline) counts every answerable case and a `reference_invalid` one as not "
        "correct; EX over valid references excludes those; reference coverage is valid "
        "references over all answerable cases. "
        + (
            "Safety accuracy and the false-refusal rate are model metrics, so a gold run "
            "reports neither."
            if gold
            else "Safety accuracy is over refusal and unanswerable cases only."
        ),
        "",
        NOT_COMPARABLE,
        "",
        "## Outcomes",
        "",
        "| Outcome | Cases |",
        "|---|---:|",
    ]
    for outcome, count in sorted(Counter(r["outcome"] for r in rows).items()):
        lines.append(f"| {outcome} | {count} |")

    invalid = _ids(rows, "reference_invalid")
    lines += ["", "## Reference-invalid cases", ""]
    lines += [f"- `{case_id}`" for case_id in invalid] if invalid else ["None."]

    outages = _ids(rows, "outage")
    if outages:
        lines += [
            "",
            "## Outages",
            "",
            f"{len(outages)} case(s) ended `outage` - a provider error that survived the retry "
            f"policy `{identity.retry_policy}`. They are outside every denominator above and "
            "keep the run incomplete until a resume completes them.",
            "",
            *[f"- `{case_id}`" for case_id in outages],
        ]

    lines += ["", "## Latency and tokens", "", *_latency_and_tokens(rows), ""]
    return "\n".join(lines)


def write_report(path: Path, manifest: Manifest, rows: Sequence[Row]) -> None:
    """Write ``render_report`` to ``path`` atomically."""
    write_atomic(path, render_report(manifest, rows))
