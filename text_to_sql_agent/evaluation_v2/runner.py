"""The v2 runner: one run directory, a per-case loop, crash resume and the outage policy.

``run_suite`` (spec §4.4):

1. computes the run's identity payload and allocates a fresh directory for it (or, with
   ``resume_dir``, re-reads the saved manifest and refuses unless the identity is unchanged);
2. writes ``manifest.json`` with ``status: "incomplete"`` **before the first case**, so an
   interrupted run is always resumable;
3. for each case not yet terminal, runs the reference through ``run_gold`` (never
   ``execute_query`` directly), runs the model through ``ask_database_with_sql`` - or, in gold
   mode, uses the reference result as the model's - scores it with ``score_v2``, and
   checkpoints the **whole** ``cases.csv`` atomically (temp file, then ``os.replace``);
4. writes ``report.md`` and the final manifest, ``complete`` only when every selected case
   has a terminal row and none is ``outage``.

The full-file checkpoint rewrites ``cases.csv`` once per case. On BIRD dev's 1,534 cases
that is 1,534 rewrites of a file that never exceeds a few hundred kilobytes - noise beside one
LLM call per case, and it means a crash at any instant leaves either the previous checkpoint
or the next one on disk, never a torn row.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import platform
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from text_to_sql_agent.config import DEFAULT_MAX_ROWS, DEFAULT_RAG_TOP_K
from text_to_sql_agent.dsn import redact_dsn
from text_to_sql_agent.engines import Engine, open_engine
from text_to_sql_agent.evaluation import run_gold
from text_to_sql_agent.evaluation_v2.contract import Case, load_suite, suite_sha256
from text_to_sql_agent.evaluation_v2.identity import (
    IdentityPayload,
    allocate_run_dir,
    config_label,
    git_state,
    identity_diff,
    retry_policy,
)
from text_to_sql_agent.evaluation_v2.manifest import (
    Manifest,
    Status,
    load_source,
    package_versions,
    read_manifest,
    source_adapter_version,
    source_release,
    write_atomic,
    write_manifest,
)
from text_to_sql_agent.evaluation_v2.report import Row, write_report
from text_to_sql_agent.evaluation_v2.scoring import SCORER_V2_VERSION, score_v2
from text_to_sql_agent.llm import _assemble_prompt
from text_to_sql_agent.pipeline import ask_database_with_sql
from text_to_sql_agent.rag import retrieve_schema_context
from text_to_sql_agent.types import QueryResult

MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.csv"
REPORT_FILE = "report.md"

# Stable: the regression gate (Task 7) aligns two runs' rows by `id`, and the Streamlit tab
# reads these names. Add columns at the end; never rename or reorder.
CSV_COLUMNS: tuple[str, ...] = (
    "suite",
    "id",
    "hardness",
    "expected",
    "outcome",
    "generated_sql",
    "error",
    "latency_ms",
    "schema_recall",
    "retrieved_tables",
    "attempts",
    "prompt_tokens",
    "completion_tokens",
)

OUTAGE = "outage"
# A gold run has nothing to run for a refusal or unanswerable case: there is no reference and
# no model, so the case is neither correct nor wrong.
NOT_APPLICABLE = "not_applicable"

# Provider failures worth retrying, and - once the retries are spent - recorded as `outage`
# rather than as a model failure: rate limits and quota (429), server errors (5xx),
# overload, timeouts, and a provider that refused the connection (a local Ollama that is
# down). The v1 script's markers, minus its bare "rate", which matched "generate".
_RETRYABLE = re.compile(
    r"\b(?:429|50[0-4])\b"
    r"|resource[_ ]exhausted|quota|rate[ _-]?limit|too many requests"
    r"|unavailable|overloaded|timed? ?out|deadline"
    r"|connect(?:ion)?(?:error| refused| reset| aborted)",
    re.IGNORECASE,
)

# SQLite's `set_progress_handler` takes a C int: a budget above 2**31 - 1 raises
# OverflowError inside every query, which would score every reference `reference_invalid`.
# Refused up front instead. (On the millisecond engines this is ~24 days - never binding.)
# A query that needs more than this runs only with the guard disabled (`work_limit=0`).
MAX_WORK_LIMIT = 2**31 - 1

Mode = Literal["gold", "llm"]
_T = TypeVar("_T")


class ResumeRefused(RuntimeError):
    """``--resume`` would mix results that do not belong together; nothing was written."""


@dataclass(frozen=True)
class RunConfig:
    """What to run. Together with the cases it determines the identity payload.

    ``subset`` is ``"full"`` with ``subset_path=None``, or a subset's name with the path of
    its committed ID list. In gold mode ``provider`` and ``model`` are recorded as ``gold``.
    ``work_limit`` (the engine's own unit; ``0`` disables the guard) and ``max_rows`` are the
    execution budget for both the reference and the model's query; ``None`` means the
    engine's ``default_work_limit`` and ``DEFAULT_MAX_ROWS`` - the app's demo guards.
    """

    suite: str
    suite_path: Path
    mode: Mode
    provider: str
    model: str
    subset: str = "full"
    subset_path: Path | None = None
    evidence: bool = False
    use_rag: bool = True
    rag_top_k: int = DEFAULT_RAG_TOP_K
    max_repair_attempts: int = 1
    max_retries: int = 0
    retry_base_seconds: float = 20.0
    work_limit: int | None = None
    max_rows: int | None = None


@dataclass(frozen=True)
class RunResult:
    """Where a run wrote, the manifest it ended with, and its rows in case order."""

    run_dir: Path
    manifest: Manifest
    rows: list[Row]


# --- selection and identity ----------------------------------------------------------------


def read_subset_ids(path: Path) -> list[str]:
    """A subset file's IDs: one per line, blank lines ignored."""
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def select_cases(config: RunConfig) -> list[Case]:
    """The suite's cases, filtered to the subset when one is named, in suite order.

    Raises:
        ValueError: If a subset ID is not in the suite, or ``subset`` and ``subset_path``
            disagree about whether this is a full run.
    """
    _check_subset_config(config)
    cases = load_suite(config.suite_path)
    if config.subset_path is None:
        return cases
    wanted = read_subset_ids(config.subset_path)
    known = {case.id for case in cases}
    unknown = [case_id for case_id in wanted if case_id not in known]
    if unknown:
        raise ValueError(
            f"{config.subset_path}: IDs not in suite '{config.suite}': {', '.join(unknown[:10])}"
        )
    chosen = set(wanted)
    return [case for case in cases if case.id in chosen]


def _check_subset_config(config: RunConfig) -> None:
    if (config.subset == "full") != (config.subset_path is None):
        raise ValueError("subset must be 'full' exactly when no subset_path is given")


def _prompt_sha256(engine: Engine) -> str:
    """SHA-256 of the system prompt ``generate_sql`` assembles for ``engine``."""
    prompt = _assemble_prompt(
        engine.prompt_dialect_section,
        engine.prompt_dialect_name,
        engine.prompt_engine_rules_block,
    )
    return hashlib.sha256(prompt.encode()).hexdigest()


def _single(values: set[_T], what: str) -> _T:
    if len(values) != 1:
        raise ValueError(f"the selected cases span engines with different {what}: {values}")
    return next(iter(values))


def build_identity(
    cases: Sequence[Case], config: RunConfig
) -> tuple[IdentityPayload, dict[str, Any]]:
    """The run's identity payload and the suite's verbatim ``source`` block.

    ``work_limit`` and ``max_rows`` are resolved here - the config's explicit values, else the
    engine's ``default_work_limit`` and ``DEFAULT_MAX_ROWS`` - and ``run_suite`` passes these
    exact values to ``run_gold`` and the pipeline, so the manifest records the budget actually
    used. A suite whose cases span engines with different prompts or default limits (and so,
    possibly, different work-limit units) is refused rather than summarised by one of them.
    """
    _check_subset_config(config)
    if config.work_limit is not None and not 0 <= config.work_limit <= MAX_WORK_LIMIT:
        raise ValueError(
            f"work_limit must be between 0 (guard disabled) and {MAX_WORK_LIMIT:,}, "
            f"got {config.work_limit:,}"
        )
    if config.max_rows is not None and config.max_rows < 1:
        raise ValueError("max_rows must be >= 1")
    source = load_source(config.suite_path)
    commit, dirty = git_state()
    engines = [open_engine(db_path) for db_path in sorted({case.db_path for case in cases})]
    prompt_sha256 = _single({_prompt_sha256(e) for e in engines}, "system prompts")
    default_work_limit = _single({e.default_work_limit for e in engines}, "work limits")
    gold = config.mode == "gold"
    payload = IdentityPayload(
        commit=commit,
        dirty=dirty,
        suite=config.suite,
        suite_sha256=suite_sha256(config.suite_path),
        subset=config.subset,
        subset_sha256=(
            hashlib.sha256(config.subset_path.read_bytes()).hexdigest()
            if config.subset_path is not None
            else ""
        ),
        source_release=source_release(source),
        adapter_version=source_adapter_version(source),
        scorer_version=SCORER_V2_VERSION,
        prompt_sha256=prompt_sha256,
        provider="gold" if gold else config.provider,
        model="gold" if gold else config.model,
        evidence=config.evidence,
        use_rag=config.use_rag,
        rag_top_k=config.rag_top_k,
        work_limit=default_work_limit if config.work_limit is None else config.work_limit,
        max_rows=DEFAULT_MAX_ROWS if config.max_rows is None else config.max_rows,
        max_repair_attempts=config.max_repair_attempts,
        retry_policy=retry_policy(config.max_retries, config.retry_base_seconds),
    )
    return payload, source


# --- one case ------------------------------------------------------------------------------


def question_text(case: Case, *, evidence: bool) -> str:
    """The question the model sees: BIRD's evidence appended only when the run says so."""
    if evidence and case.evidence:
        return f"{case.question}\n\nEvidence: {case.evidence}"
    return case.question


def _error_text(exc: Exception) -> str:
    return redact_dsn(f"{type(exc).__name__}: {exc}")


def _reference(case: Case, config: RunConfig) -> QueryResult:
    """Run the case's gold SQL through ``run_gold`` (which applies ``is_safe_query``).

    ``run_gold`` returns ``GOLD_SQL_UNSAFE`` for a refused reference but *raises* when a safe
    reference fails to execute. Either way the reference is invalid; the exception becomes a
    redacted error result so ``score_v2`` scores the case ``reference_invalid``.
    """
    try:
        _, result = run_gold(
            {"gold_sql": case.gold_sql, "db_path": case.db_path},
            work_limit=config.work_limit,
            max_rows=config.max_rows,
        )
    except Exception as exc:
        return QueryResult(columns=[], rows=[], sql=case.gold_sql, error=_error_text(exc))
    return result


def _is_generation_failure(sql: str, result: QueryResult) -> bool:
    """Whether ``ask_database_with_sql`` failed inside ``generate_sql`` itself.

    That path - and only that one - returns ``""`` with a result that carries no SQL. A
    refused or failed *query* always carries its SQL, so an execution error that happens to
    contain "429" or "timeout" can never be mistaken for a provider outage.
    """
    return sql == "" and result.sql is None and result.error is not None


def _ask_model(
    case: Case, question: str, config: RunConfig
) -> tuple[str, QueryResult, int, bool, float]:
    """Run the pipeline under the retry policy: ``(sql, result, attempts, outage, ms)``.

    ``ms`` is the latency of the final attempt alone - backoff sleeps and earlier failed
    attempts are excluded, so latency describes the pipeline rather than the provider's
    rate limiter.
    """
    attempt = 0
    while True:
        started = time.perf_counter()
        try:
            sql, result = ask_database_with_sql(
                question,
                db_path=case.db_path,
                model_name=config.model,
                provider=config.provider,
                use_rag=config.use_rag,
                rag_top_k=config.rag_top_k,
                max_repair_attempts=config.max_repair_attempts,
                work_limit=config.work_limit,
                max_rows=config.max_rows,
            )
        except Exception as exc:
            # A harness-side failure outside generation (unreachable database, schema
            # retrieval): recorded as the case's error, never retried, never an outage.
            elapsed = (time.perf_counter() - started) * 1000
            return "", QueryResult(columns=[], rows=[], error=_error_text(exc)), 1, False, elapsed
        elapsed = (time.perf_counter() - started) * 1000
        if not _is_generation_failure(sql, result) or not _RETRYABLE.search(result.error or ""):
            # A provider error that does *not* look transient - an unknown provider, a missing
            # SDK, a rejected API key, an unknown model - is `error`, not `outage`. Retrying
            # or resuming would reproduce it identically, so as an outage it would keep the run
            # incomplete forever; as an error it is counted and visible in the outcome table,
            # where a misconfigured run shows as every case failing.
            return sql, result, attempt + 1, False, elapsed
        if attempt >= config.max_retries:
            return sql, result, attempt + 1, True, elapsed
        time.sleep(config.retry_base_seconds * (2**attempt))
        attempt += 1


def _schema_recall(case: Case, question: str, config: RunConfig) -> tuple[str, str]:
    """``(recall, retrieved_tables)`` as CSV strings; recall blank when undefined.

    Recall is defined only under RAG - without it the model is shown the whole schema - and
    only when the case lists ``expected_tables``. Both sides are lowercased: the adapters
    lowercase ``expected_tables``, while ``qualified_name`` keeps the database's spelling.
    """
    if not config.use_rag:
        return "", ""
    try:
        chunks = retrieve_schema_context(case.db_path, question, top_k=config.rag_top_k).chunks
    except Exception:
        # The pipeline call reports the same failure in this row's `error`; recall is blank.
        return "", ""
    retrieved = sorted({chunk.qualified_name.lower() for chunk in chunks})
    if not case.expected_tables:
        return "", ", ".join(retrieved)
    expected = {table.lower() for table in case.expected_tables}
    recall = len(expected & set(retrieved)) / len(expected)
    return str(round(recall, 3)), ", ".join(retrieved)


def evaluate_case(case: Case, config: RunConfig) -> Row:
    """Run and score one case, returning its ``cases.csv`` row (every value a string)."""
    question = question_text(case, evidence=config.evidence)
    started = time.perf_counter()
    gold_result = _reference(case, config) if case.expected == "answerable" else None
    gold_ms = (time.perf_counter() - started) * 1000

    generated_sql = ""
    error: str | None = None
    attempts = ""
    latency = ""
    if config.mode == "gold":
        if gold_result is None:
            outcome = NOT_APPLICABLE
        else:
            generated_sql = case.gold_sql
            outcome = score_v2(case, gold_result, gold_result)
            error = gold_result.error
            latency = f"{gold_ms:.2f}"
    else:
        generated_sql, result, tries, outage, ms = _ask_model(case, question, config)
        outcome = OUTAGE if outage else score_v2(case, result, gold_result)
        # For an invalid reference the reference's error is the one that explains the row.
        error = (
            gold_result.error
            if outcome == "reference_invalid" and gold_result is not None
            else result.error
        )
        attempts = str(tries)
        latency = f"{ms:.2f}"

    recall, retrieved = _schema_recall(case, question, config)
    return {
        "suite": case.suite,
        "id": case.id,
        "hardness": case.hardness,
        "expected": case.expected,
        "outcome": outcome,
        "generated_sql": generated_sql,
        # Errors may echo a DSN a driver failed to reach, password included.
        "error": redact_dsn(error or ""),
        "latency_ms": latency,
        "schema_recall": recall,
        "retrieved_tables": retrieved,
        "attempts": attempts,
        # `generate_sql` returns text only, so no provider reports usage through this
        # interface yet. Blank means "not reported", never zero.
        "prompt_tokens": "",
        "completion_tokens": "",
    }


# --- files ---------------------------------------------------------------------------------


def write_rows(path: Path, rows: Sequence[Row]) -> None:
    """Rewrite the whole ``cases.csv`` atomically (``cases.csv.tmp``, then ``os.replace``)."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    write_atomic(path, buffer.getvalue())


def read_rows(path: Path) -> list[Row]:
    """Read ``cases.csv``. Raises ``ValueError`` if its header is not ``CSV_COLUMNS``."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise ValueError(f"{path}: columns {reader.fieldnames} are not {list(CSV_COLUMNS)}")
        return list(reader)


def _load_for_resume(
    run_dir: Path, payload: IdentityPayload, config: RunConfig, selected: Sequence[str]
) -> tuple[dict[str, Any], dict[str, Row]]:
    """The saved manifest and rows, after every check that makes resuming safe."""
    manifest_path = run_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        raise ResumeRefused(f"{run_dir}: no {MANIFEST_FILE}, so not a v2 run directory")
    try:
        saved = read_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        raise ResumeRefused(f"{manifest_path}: unreadable manifest: {exc}") from exc
    if saved.get("status") == "complete":
        raise ResumeRefused(f"{run_dir}: the run is already complete; start a new run instead")
    if not isinstance(saved.get("started"), str) or not isinstance(
        saved.get("duration_s"), int | float
    ):
        raise ResumeRefused(f"{manifest_path}: 'started' or 'duration_s' is missing or ill-typed")
    differing = identity_diff(saved, payload)
    if saved.get("mode") != config.mode:
        differing.append("mode")
    if differing:
        now = {**dataclasses.asdict(payload), "mode": config.mode}
        detail = "; ".join(
            f"{name} (saved {saved.get(name, '<missing>')!r}, now {now[name]!r})"
            for name in differing
        )
        raise ResumeRefused(f"{run_dir}: identity differs from the saved manifest: {detail}")

    rows: dict[str, Row] = {}
    cases_path = run_dir / CASES_FILE
    if cases_path.is_file():
        try:
            saved_rows = read_rows(cases_path)
        except (OSError, ValueError) as exc:
            raise ResumeRefused(f"{cases_path}: unreadable: {exc}") from exc
        chosen = set(selected)
        for row in saved_rows:
            if row["id"] in rows:
                raise ResumeRefused(f"{cases_path}: case '{row['id']}' appears twice")
            rows[row["id"]] = row
        unknown = [case_id for case_id in rows if case_id not in chosen]
        if unknown:
            raise ResumeRefused(
                f"{cases_path}: saved rows are not in the selected cases: {', '.join(unknown[:10])}"
            )
    return saved, rows


def run_suite(
    cases: Sequence[Case],
    *,
    config: RunConfig,
    out_root: Path,
    resume_dir: Path | None = None,
) -> RunResult:
    """Run ``cases`` into a new directory under ``out_root``, or resume ``resume_dir``.

    On resume: every saved terminal row is kept, and the cases that are unattempted or
    ``outage`` are run, each exactly once.

    Raises:
        ResumeRefused: If ``resume_dir`` is not an incomplete run of the same identity, or
            holds rows for cases outside ``cases``. Nothing is written.
        ValueError: If ``cases`` is empty, repeats an ID, belongs to another suite, or the
            suite's ``.source.json`` is missing or malformed. Nothing is written.
    """
    cases = list(cases)
    if not cases:
        raise ValueError("no cases selected")
    foreign = sorted({case.suite for case in cases if case.suite != config.suite})
    if foreign:
        raise ValueError(f"cases belong to suite(s) {foreign}, not '{config.suite}'")
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("selected cases repeat an ID")

    payload, source = build_identity(cases, config)
    # From here on the budget is explicit: the identity's resolved values are the ones passed
    # to every reference and model query, never a default looked up again later.
    config = dataclasses.replace(config, work_limit=payload.work_limit, max_rows=payload.max_rows)
    session_started = time.monotonic()
    if resume_dir is None:
        started = datetime.now(UTC).replace(microsecond=0)
        label = config_label(
            use_rag=config.use_rag, rag_top_k=config.rag_top_k, evidence=config.evidence
        )
        run_dir = allocate_run_dir(out_root, payload, started=started, config=label)
        started_iso = started.isoformat()
        prior_duration = 0.0
        rows: dict[str, Row] = {}
    else:
        run_dir = resume_dir
        saved, rows = _load_for_resume(run_dir, payload, config, ids)
        started_iso = str(saved["started"])
        prior_duration = float(saved["duration_s"])

    python, packages = platform.python_version(), package_versions()

    def manifest(status: Status, outages: int) -> Manifest:
        return Manifest(
            identity=payload,
            run_id=run_dir.name,
            mode=config.mode,
            case_count=len(cases),
            source=source,
            started=started_iso,
            duration_s=round(prior_duration + time.monotonic() - session_started, 3),
            outage_count=outages,
            status=status,
            python=python,
            packages=packages,
        )

    def outage_count() -> int:
        return sum(1 for row in rows.values() if row["outcome"] == OUTAGE)

    manifest_path = run_dir / MANIFEST_FILE
    write_manifest(manifest_path, manifest("incomplete", outage_count()))

    for case in cases:
        saved_row = rows.get(case.id)
        if saved_row is not None and saved_row["outcome"] != OUTAGE:
            continue
        rows[case.id] = evaluate_case(case, config)
        write_rows(run_dir / CASES_FILE, [rows[i] for i in ids if i in rows])
        # Still `incomplete`: refreshed only so an interrupted session keeps its elapsed time
        # and outage count for the resume that follows.
        write_manifest(manifest_path, manifest("incomplete", outage_count()))

    ordered = [rows[i] for i in ids]
    outages = sum(1 for row in ordered if row["outcome"] == OUTAGE)
    final = manifest("complete" if outages == 0 else "incomplete", outages)
    write_report(run_dir / REPORT_FILE, final, ordered)
    write_manifest(manifest_path, final)
    return RunResult(run_dir=run_dir, manifest=final, rows=ordered)
