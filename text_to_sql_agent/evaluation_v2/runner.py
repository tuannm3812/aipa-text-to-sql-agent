"""The v2 runner: one run directory, a per-case loop, crash resume and the outage policy.

``run_suite`` (spec §4.4):

1. computes the run's identity payload and allocates a fresh directory for it (or, with
   ``resume_dir``, re-reads the saved manifest and refuses unless the identity is unchanged);
2. writes ``manifest.json`` with ``status: "incomplete"`` **before the first case**, so an
   interrupted run is always resumable;
3. for each case not yet terminal, runs the reference through ``run_gold`` (never
   ``execute_query`` directly), runs the model through ``ask_database_with_sql`` - or, in gold
   mode, uses the reference result as the model's - scores it with ``score_v2``, and
   checkpoints the **whole** ``cases.csv`` atomically (temp file, then ``os.replace``). A
   re-run ``outage`` row keeps the tokens spent before it: they are added to the new row's;
4. writes ``report.md`` and the final manifest, ``complete`` only when every selected case
   has a terminal row and none is ``outage``.

A session holds an exclusive lock file (``.lock``, created with ``O_CREAT | O_EXCL``, holding
``<pid> <host> <token>``) for the whole loop, so two concurrent ``--resume`` sessions on one run
cannot both proceed, and releases it only if it still holds its own token. A lock left by a
killed process on this host is taken over with a warning, under a separate takeover mutex and
an atomic replace, so the lock path is never absent; anything less certain is refused, naming
the pid and the reason.

Relative ``db_path`` values are resolved against the project root (``identity.REPO_ROOT``,
the directory holding this package): every suite writes them repo-relative, so a run started
from another working directory reads the same databases instead of failing to open any. Each
distinct database is checked reachable before a directory is allocated.

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
import os
import platform
import re
import socket
import time
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args

from text_to_sql_agent.config import DEFAULT_MAX_ROWS, DEFAULT_RAG_TOP_K
from text_to_sql_agent.dsn import redact_dsn
from text_to_sql_agent.engines import Engine, open_engine
from text_to_sql_agent.evaluation import run_gold
from text_to_sql_agent.evaluation_v2.contract import Case, load_suite, suite_sha256
from text_to_sql_agent.evaluation_v2.identity import (
    REPO_ROOT,
    IdentityPayload,
    allocate_run_dir,
    config_label,
    database_fingerprint,
    fingerprint_changes,
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
from text_to_sql_agent.evaluation_v2.scoring import (
    EMPTY_GENERATED_SQL,
    SCORER_V2_VERSION,
    Outcome,
    score_v2,
)
from text_to_sql_agent.llm import TokenUsage, _assemble_prompt, usage_scope
from text_to_sql_agent.pipeline import ask_database_with_sql
from text_to_sql_agent.rag import retrieve_schema_context
from text_to_sql_agent.types import QueryResult

MANIFEST_FILE = "manifest.json"
CASES_FILE = "cases.csv"
REPORT_FILE = "report.md"
LOCK_FILE = ".lock"

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
    "generation_failure",
)
# Provider-reported counts: a whole number, or blank when the provider reported nothing.
TOKEN_COLUMNS: tuple[str, ...] = ("prompt_tokens", "completion_tokens")

OUTAGE = "outage"
# A gold run has nothing to run for a refusal or unanswerable case: there is no reference and
# no model, so the case is neither correct nor wrong.
NOT_APPLICABLE = "not_applicable"

# The outcomes a saved row may carry, per mode. A gold run self-compares references, so it
# can produce only these four; a model run can produce every `Outcome`, `outage` included.
_ALLOWED_OUTCOMES: dict[str, frozenset[str]] = {
    "llm": frozenset(get_args(Outcome)),
    "gold": frozenset({"correct", "wrong", "reference_invalid", NOT_APPLICABLE}),
}

# The documented budget for public suites, raised on 2026-10-09 from 100,000,000 steps and
# 50,000 rows once the full BIRD dev set (not just subset200) was gated. Quoted when a public
# suite is run without an explicit budget - it is a documented convention, not a code default.
PUBLIC_SUITE_BUDGET = "--work-limit 1000000000 --max-rows 100000"

# Provider failures worth retrying, and - once the retries are spent - recorded as `outage`
# rather than as a model failure: rate limits and quota (429), server errors (5xx),
# overload, timeouts, and a provider that refused the connection (a local Ollama that is
# down). The v1 script's markers, minus its bare "rate", which matched "generate". A bare
# "unavailable" is deliberately absent: "model unavailable" can be permanent (a retired model)
# and would then be an outage forever; a 503 or "overloaded" beside it is what makes it
# transient, and those match on their own.
_RETRYABLE = re.compile(
    r"\b(?:429|50[0-4])\b"
    r"|resource[_ ]exhausted|quota|rate[ _-]?limit|too many requests"
    r"|overloaded|timed? ?out|deadline"
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
        ValueError: If the file's suite is not ``config.suite``, a subset ID is not in the
            suite, or ``subset`` and ``subset_path`` disagree about whether this is a full run.
    """
    _check_subset_config(config)
    cases = load_suite(config.suite_path)
    if cases[0].suite != config.suite:  # `load_suite` guarantees one suite per file
        raise ValueError(
            f"{config.suite_path} holds suite '{cases[0].suite}', not '{config.suite}'"
        )
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


def resolve_db_path(db_path: str) -> str:
    """A suite's ``db_path`` made independent of the working directory.

    A DSN (``scheme://...``) or an absolute path is returned unchanged; a relative path is
    resolved against the project root, where every suite's relative paths are rooted.
    """
    if "://" in db_path or Path(db_path).is_absolute():
        return db_path
    return str(REPO_ROOT / db_path)


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

    Every database the cases query is hashed into ``database_fingerprint``, after the
    reachability check, so a resume - which recomputes this payload - refuses a database whose
    contents changed since the run started, and two runs over different contents are
    incompatible in the regression gate.
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
    if source["kind"] == "download" and (config.work_limit is None or config.max_rows is None):
        raise ValueError(
            f"suite '{config.suite}' is a public benchmark: give an explicit execution budget "
            f"({PUBLIC_SUITE_BUDGET} is the documented one). The engine defaults are the "
            "hosted demo's guards and would abort valid references."
        )
    commit, dirty = git_state()
    engines = []
    for db_path in sorted({case.db_path for case in cases}):
        engine = open_engine(db_path)
        try:
            engine.check_reachable()
        except Exception as exc:
            # Refused before any directory exists: otherwise every reference is
            # `reference_invalid` and the run completes, citable, at EX 0 % under the same
            # identity hash as a healthy run.
            raise ValueError(
                f"database {redact_dsn(db_path)} is unreachable: {_error_text(exc)}"
            ) from exc
        engines.append(engine)
    fingerprint = database_fingerprint(case.db_path for case in cases)
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
        database_fingerprint=fingerprint,
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
) -> tuple[str, QueryResult, int, bool, bool, float]:
    """Run the pipeline under the retry policy.

    Returns ``(sql, result, attempts, outage, generation_failure, ms)``. ``generation_failure``
    is true when the case ended without the model answering: generation raised (and the
    error was not an outage), or the harness raised around the pipeline.

    ``ms`` is the latency of the final attempt alone - backoff sleeps and earlier failed
    attempts are excluded, so latency describes the pipeline rather than the provider's
    rate limiter.
    """
    attempt = 0
    while True:
        started = time.perf_counter()
        repair_errors: list[Exception] = []
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
                on_repair_error=repair_errors.append,
            )
        except Exception as exc:
            # A harness-side failure outside generation (unreachable database, schema
            # retrieval): recorded as the case's error, never retried, never an outage.
            elapsed = (time.perf_counter() - started) * 1000
            failed = QueryResult(columns=[], rows=[], error=_error_text(exc))
            return "", failed, 1, False, True, elapsed
        elapsed = (time.perf_counter() - started) * 1000
        # A provider failure is either the first generation raising, or the *repair* call
        # raising after the first SQL failed - the pipeline swallows the latter and returns
        # the execution error, which would otherwise read as the model's fault.
        provider_error: str | None = None
        if _is_generation_failure(sql, result):
            provider_error = result.error
        elif repair_errors:
            provider_error = f"repair: {_error_text(repair_errors[-1])}"
        if provider_error is None or not _RETRYABLE.search(provider_error):
            # A provider error that does *not* look transient - an unknown provider, a missing
            # SDK, a rejected API key, an unknown model - is `error`, not `outage`. Retrying
            # or resuming would reproduce it identically, so as an outage it would keep the run
            # incomplete forever; as an error it is counted and visible in the outcome table,
            # and a run in which no case produced SQL is not citable (`Manifest.citable`).
            generation_failed = _is_generation_failure(sql, result)
            return sql, result, attempt + 1, False, generation_failed, elapsed
        if attempt >= config.max_retries:
            outage = QueryResult(columns=[], rows=[], sql=result.sql, error=provider_error)
            return sql, outage, attempt + 1, True, False, elapsed
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
    generation_failure = False
    usage = TokenUsage()
    if config.mode == "gold":
        if gold_result is None:
            outcome = NOT_APPLICABLE
        else:
            generated_sql = case.gold_sql
            outcome = score_v2(case, gold_result, gold_result, generated_sql=case.gold_sql)
            error = gold_result.error
            latency = f"{gold_ms:.2f}"
    else:
        # One scope over every attempt: a retried call is still spent tokens, and so are the
        # ones counted before a failure.
        with usage_scope() as usage:
            generated_sql, result, tries, outage, generation_failure, ms = _ask_model(
                case, question, config
            )
        outcome = (
            OUTAGE if outage else score_v2(case, result, gold_result, generated_sql=generated_sql)
        )
        # For an invalid reference the reference's error is the one that explains the row.
        error = (
            gold_result.error
            if outcome == "reference_invalid" and gold_result is not None
            else result.error
        )
        if outcome == "error" and not generation_failure and not generated_sql.strip():
            # The model answered with nothing. Not the BLOCKED_UNSAFE_SQL that
            # `query_refusal("")` put in the result: that code means a refusal.
            error = EMPTY_GENERATED_SQL
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
        # Summed by `usage_scope` over the generation, every repair and every retry, and by a
        # resume over the sessions before an outage (`_keep_spent_tokens`). Blank means "not
        # reported" (gold runs, or a provider that gave no usage), never zero.
        "prompt_tokens": "" if usage.prompt_tokens is None else str(usage.prompt_tokens),
        "completion_tokens": (
            "" if usage.completion_tokens is None else str(usage.completion_tokens)
        ),
        "generation_failure": "1" if generation_failure else "",
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


class StaleLockWarning(UserWarning):
    """A run's lock was left by a process that no longer exists and was taken over."""


TAKEOVER_SUFFIX = ".takeover"


def _new_lock_line() -> str:
    """``<pid> <host> <token>``: the token tells two sessions with one pid apart."""
    return f"{os.getpid()} {socket.gethostname()} {os.urandom(16).hex()}\n"


def _read_lock(path: Path) -> str:
    """The lock file's text. Raises ``FileNotFoundError`` when there is none."""
    return path.read_text(encoding="utf-8")


def _holder_pid(text: str) -> str:
    parts = text.split()
    return parts[0] if parts else "unknown"


def _why_not_stale(text: str) -> str | None:
    """``None`` when a lock's holder is provably gone; otherwise why it must be kept.

    Provably gone means: the lock is in the ``<pid> <host> <token>`` format, was taken on
    this host, and its pid does not exist. Anything unprovable - another host, an
    unparseable or older-format lock, a pid owned by another user (``PermissionError``) -
    keeps the lock, so it is never broken on a guess.
    """
    parts = text.split()
    if len(parts) != 3 or not parts[0].isdigit():
        return (
            "the lock is not in the '<pid> <host> <token>' format (an older runner wrote it, "
            "or it is damaged), so its holder cannot be checked"
        )
    if parts[1] != socket.gethostname():
        return f"it was taken on another host ({parts[1]}), where its pid cannot be checked"
    pid = int(parts[0])
    if pid <= 0 or pid == os.getpid():
        return f"pid {pid} is still running (this process)"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError:
        return f"pid {pid} is still running (owned by another user)"
    return f"pid {pid} is still running"


def _take_over_stale_lock(path: Path, seen: str, mine: str) -> bool:
    """Replace the stale lock ``seen`` with ``mine``; ``False`` if the lock has vanished.

    The canonical lock path is never absent during a takeover, and a takeover only ever
    replaces the exact stale lock it observed:

    1. take the short-lived mutex ``<lock>.takeover`` with ``O_CREAT | O_EXCL`` - a held
       mutex means another session is recovering, and this one refuses;
    2. while holding it, re-read the lock: proceed only if it is byte-identical to ``seen``
       and its holder is still provably gone;
    3. write ``mine`` to a unique temp file and ``os.replace`` it over the lock, which is
       atomic, so a third session's ``O_EXCL`` create fails throughout;
    4. release the mutex (only if it still holds this session's line).

    If writing this session's line into the freshly created mutex fails (``ENOSPC``, a
    ``KeyboardInterrupt``), the mutex is unlinked before the error propagates: an empty
    mutex matches no session's line, so ``_remove_if_owned`` could never clear it and every
    later recovery would be refused until someone deleted it by hand - the same reason
    ``_session_lock`` removes an empty ``.lock``.

    Assumptions and known limits:

    - "Stale" means *same hostname and a pid that does not exist*. That holds only when every
      process that could hold the lock shares one pid namespace per hostname. It does not hold
      across pid namespaces that share a hostname - for example containers run with
      ``--network host`` (same hostname, separate pid namespaces) on one run directory: a live
      holder in another container is invisible to ``os.kill(pid, 0)`` here and would be taken
      for dead. Do not share a run directory across such containers.
    - Every step above is atomic against another *session*, but not against a human. Deleting
      ``.lock`` or ``.lock.takeover`` by hand while a session is recovering - the refusal
      messages suggest it only when no session is - can let a second session in during that
      window. The locks protect sessions from each other, not from manual intervention.

    Raises:
        ResumeRefused: The mutex is held, the lock changed since it was observed, or its
            holder is no longer provably gone. Refusing an uncertain recovery is preferable to
            two sessions writing one run.
    """
    mutex = path.with_name(path.name + TAKEOVER_SUFFIX)
    try:
        fd = os.open(mutex, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise ResumeRefused(
            f"{path.parent}: another session is recovering its stale lock ({mutex} exists); "
            f"if no session is, delete {mutex} and resume again"
        ) from None
    try:
        os.write(fd, mine.encode())
    except BaseException:
        os.close(fd)
        mutex.unlink(missing_ok=True)  # ours, and empty: no other session can own it
        raise
    os.close(fd)
    try:
        try:
            current = _read_lock(path)
        except FileNotFoundError:
            return False  # removed by hand meanwhile: an ordinary exclusive create will do
        if current != seen:
            raise ResumeRefused(
                f"{path.parent}: the lock changed while recovering it (now pid "
                f"{_holder_pid(current)}, {path}); another session holds the run"
            )
        reason = _why_not_stale(current)
        if reason is not None:
            raise ResumeRefused(f"{path.parent}: not taking over the lock {path}: {reason}")
        write_atomic(path, mine)
        return True
    finally:
        _remove_if_owned(mutex, mine)


def _remove_if_owned(path: Path, mine: str) -> None:
    """Unlink ``path`` only if it holds exactly ``mine``: never another session's file."""
    try:
        if _read_lock(path) == mine:
            path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _session_lock(run_dir: Path) -> Iterator[None]:
    """Hold ``run_dir/.lock`` for one session; refuse if a live session holds it.

    ``O_CREAT | O_EXCL`` makes taking a free lock atomic. The lock records
    ``<pid> <host> <token>``, with a fresh random token per session. It is released on every
    exit path, ``KeyboardInterrupt`` included - and only if it still holds this session's own
    line, so a session never removes a lock it does not own. A lock left by a hard kill is
    taken over - with a ``StaleLockWarning`` - only when its holder is provably gone (same
    host, pid not running), under the protocol in ``_take_over_stale_lock``. Otherwise the
    refusal names the pid and the reason.
    """
    path = run_dir / LOCK_FILE
    mine = _new_lock_line()
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                holder = _read_lock(path)
            except FileNotFoundError:
                continue  # released between our open and our read: try again
            except OSError:
                holder = ""
            reason = _why_not_stale(holder)
            if reason is not None:
                raise ResumeRefused(
                    f"{run_dir} is locked by another session (pid {_holder_pid(holder)}, "
                    f"{path}): {reason}; if that process is no longer running the lock is "
                    "stale - delete the file and resume again"
                ) from None
            if not _take_over_stale_lock(path, holder, mine):
                continue
            warnings.warn(
                f"{path}: took over a stale lock left by pid {_holder_pid(holder)}, which "
                "is no longer running on this host",
                StaleLockWarning,
                stacklevel=3,
            )
            break
        try:
            os.write(fd, mine.encode())
        except BaseException:
            # The file is ours but empty; an empty lock counts as live, so remove it now.
            os.close(fd)
            path.unlink(missing_ok=True)
            raise
        os.close(fd)
        break
    else:
        raise ResumeRefused(f"{run_dir}: could not take the lock {path}; try again")
    try:
        yield
    finally:
        _remove_if_owned(path, mine)


def _check_saved_row(row: Row, case: Case, mode: str, cases_path: Path) -> None:
    """Refuse a saved row that the runner could not have written for this case and mode."""
    allowed = _ALLOWED_OUTCOMES[mode]
    if row["outcome"] not in allowed:
        raise ResumeRefused(
            f"{cases_path}: row '{case.id}' has outcome {row['outcome']!r}, which a {mode} "
            f"run never writes (allowed: {', '.join(sorted(allowed))})"
        )
    allowed_flags = {""} if mode == "gold" else {"", "1"}
    if row["generation_failure"] not in allowed_flags:
        raise ResumeRefused(
            f"{cases_path}: row '{case.id}' has generation_failure "
            f"{row['generation_failure']!r}, which a {mode} run never writes"
        )
    for field in ("suite", "expected", "hardness"):
        if row[field] != getattr(case, field):
            raise ResumeRefused(
                f"{cases_path}: row '{case.id}' has {field} {row[field]!r}, but the case "
                f"says {getattr(case, field)!r}"
            )
    # Checked here, before anything runs: a resumed outage adds its saved counts to the new
    # attempt's (`_keep_spent_tokens`), and the report sums every row's.
    for field in TOKEN_COLUMNS:
        cell = row[field]
        if cell and not (cell.isascii() and cell.isdigit()):
            raise ResumeRefused(
                f"{cases_path}: row '{case.id}' has {field} {cell!r}, which the runner never "
                "writes (a whole number of tokens, or blank)"
            )


def _saved_vs_now(name: str, saved: dict[str, Any], now: dict[str, Any]) -> str:
    """One differing identity field, described for a resume refusal.

    A changed database fingerprint names the databases that changed rather than printing both
    full lists.
    """
    if name == "database_fingerprint" and name in saved:
        try:
            return f"{name} changed for {', '.join(fingerprint_changes(saved[name], now[name]))}"
        except (TypeError, ValueError):
            pass  # a malformed saved value: show it as it is
    return f"{name} (saved {saved.get(name, '<missing>')!r}, now {now[name]!r})"


def _load_for_resume(
    run_dir: Path, payload: IdentityPayload, config: RunConfig, cases: Sequence[Case]
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
    now: dict[str, Any] = {
        **dataclasses.asdict(payload),
        "mode": config.mode,
        "case_count": len(cases),
    }
    differing += [name for name in ("mode", "case_count") if saved.get(name) != now[name]]
    if differing:
        detail = "; ".join(_saved_vs_now(name, saved, now) for name in differing)
        raise ResumeRefused(f"{run_dir}: identity differs from the saved manifest: {detail}")

    rows: dict[str, Row] = {}
    cases_path = run_dir / CASES_FILE
    if cases_path.is_file():
        try:
            saved_rows = read_rows(cases_path)
        except (OSError, ValueError) as exc:
            raise ResumeRefused(f"{cases_path}: unreadable: {exc}") from exc
        by_id = {case.id: case for case in cases}
        for row in saved_rows:
            if row["id"] in rows:
                raise ResumeRefused(f"{cases_path}: case '{row['id']}' appears twice")
            rows[row["id"]] = row
        unknown = [case_id for case_id in rows if case_id not in by_id]
        if unknown:
            raise ResumeRefused(
                f"{cases_path}: saved rows are not in the selected cases: {', '.join(unknown[:10])}"
            )
        for case_id, row in rows.items():
            _check_saved_row(row, by_id[case_id], config.mode, cases_path)
    return saved, rows


def _check_selection(cases: Sequence[Case], config: RunConfig) -> None:
    """Refuse ``cases`` unless they are exactly ``select_cases(config)``, in order.

    The identity names a suite and a subset; a caller-supplied list that differs from that
    selection would be recorded - and could complete, citable - under a name it does not match.
    """
    expected = select_cases(config)
    if list(cases) == expected:
        return
    given_ids = [case.id for case in cases]
    expected_ids = [case.id for case in expected]
    if given_ids != expected_ids:
        missing = [i for i in expected_ids if i not in set(given_ids)]
        extra = [i for i in given_ids if i not in set(expected_ids)]
        raise ValueError(
            f"cases are not the selection for suite '{config.suite}' subset '{config.subset}': "
            f"{len(given_ids)} given, {len(expected_ids)} selected; missing "
            f"{', '.join(missing[:10]) or 'none'}; extra {', '.join(extra[:10]) or 'none'}"
            + ("; same IDs in a different order" if not missing and not extra else "")
        )
    changed = [g.id for g, e in zip(cases, expected, strict=True) if g != e]
    raise ValueError(f"cases differ from the suite file's records: {', '.join(changed[:10])}")


def _generation_failures(rows: Sequence[Row]) -> int:
    """Terminal rows that ended before the model answered (``generation_failure`` set)."""
    return sum(1 for row in rows if row["outcome"] != OUTAGE and row["generation_failure"])


def _add_token_cells(earlier: str, latest: str) -> str:
    """Two token cells added, keeping blank ("not reported") apart from an explicit ``0``.

    Blank plus blank stays blank; a count plus blank is that count; two counts are summed.
    """
    if not earlier:
        return latest
    if not latest:
        return earlier
    return str(int(earlier) + int(latest))


def _keep_spent_tokens(outage_row: Row, row: Row) -> Row:
    """``row``, which replaces a saved ``outage`` row, with that row's token counts added.

    Tokens a session spent on the case before its outage were spent in this run, so a resume
    keeps them in the case's totals, prompt and completion each on its own. The saved cells
    already hold every earlier session's sum, so each resume adds the previous total exactly
    once. Every other cell, the outcome included, is the new attempt's.
    """
    return row | {name: _add_token_cells(outage_row[name], row[name]) for name in TOKEN_COLUMNS}


def run_suite(
    cases: Sequence[Case],
    *,
    config: RunConfig,
    out_root: Path,
    resume_dir: Path | None = None,
) -> RunResult:
    """Run ``cases`` into a new directory under ``out_root``, or resume ``resume_dir``.

    ``cases`` must be exactly ``select_cases(config)``. On resume: every saved terminal row is
    kept, and the cases that are unattempted or ``outage`` are run, each exactly once. An
    ``outage`` row's token counts are added to the row that replaces it.

    Raises:
        ResumeRefused: If ``resume_dir`` is locked by another session, is not an incomplete
            run of the same identity and case count, or holds rows that are foreign to
            ``cases`` or that this mode never writes. Nothing is written.
        ValueError: If ``cases`` is not the configured selection, the budget is unusable or
            missing for a public suite, a database is unreachable, or the suite's
            ``.source.json`` is missing or malformed. Nothing is written.
    """
    cases = list(cases)
    if not cases:
        raise ValueError("no cases selected")
    _check_selection(cases, config)
    cases = [dataclasses.replace(case, db_path=resolve_db_path(case.db_path)) for case in cases]

    payload, source = build_identity(cases, config)
    # From here on the budget is explicit: the identity's resolved values are the ones passed
    # to every reference and model query, never a default looked up again later.
    config = dataclasses.replace(config, work_limit=payload.work_limit, max_rows=payload.max_rows)
    if resume_dir is None:
        started = datetime.now(UTC).replace(microsecond=0)
        label = config_label(
            use_rag=config.use_rag, rag_top_k=config.rag_top_k, evidence=config.evidence
        )
        run_dir = allocate_run_dir(out_root, payload, started=started, config=label)
    else:
        run_dir = resume_dir
        if not run_dir.is_dir():
            raise ResumeRefused(f"{run_dir}: not a directory")

    with _session_lock(run_dir):
        if resume_dir is None:
            started_iso = started.isoformat()
            prior_duration = 0.0
            rows: dict[str, Row] = {}
        else:
            saved, rows = _load_for_resume(run_dir, payload, config, cases)
            started_iso = str(saved["started"])
            prior_duration = float(saved["duration_s"])
        return _run_locked(
            cases, config, payload, source, run_dir, rows, started_iso, prior_duration
        )


def _run_locked(
    cases: list[Case],
    config: RunConfig,
    payload: IdentityPayload,
    source: dict[str, Any],
    run_dir: Path,
    rows: dict[str, Row],
    started_iso: str,
    prior_duration: float,
) -> RunResult:
    """The case loop and the final writes, run while ``_session_lock`` is held."""
    ids = [case.id for case in cases]
    session_started = time.monotonic()
    python, packages = platform.python_version(), package_versions()

    def manifest(status: Status) -> Manifest:
        done = [rows[i] for i in ids if i in rows]
        return Manifest(
            identity=payload,
            run_id=run_dir.name,
            mode=config.mode,
            case_count=len(cases),
            source=source,
            started=started_iso,
            duration_s=round(prior_duration + time.monotonic() - session_started, 3),
            outage_count=sum(1 for row in done if row["outcome"] == OUTAGE),
            status=status,
            generation_failures=_generation_failures(done),
            python=python,
            packages=packages,
        )

    manifest_path = run_dir / MANIFEST_FILE
    write_manifest(manifest_path, manifest("incomplete"))

    for case in cases:
        saved_row = rows.get(case.id)
        if saved_row is not None and saved_row["outcome"] != OUTAGE:
            continue
        row = evaluate_case(case, config)
        # A saved row reaching here is an outage being retried: its spent tokens carry over.
        rows[case.id] = row if saved_row is None else _keep_spent_tokens(saved_row, row)
        write_rows(run_dir / CASES_FILE, [rows[i] for i in ids if i in rows])
        # Still `incomplete`: refreshed only so an interrupted session keeps its elapsed time
        # and outage count for the resume that follows.
        write_manifest(manifest_path, manifest("incomplete"))

    ordered = [rows[i] for i in ids]
    outages = sum(1 for row in ordered if row["outcome"] == OUTAGE)
    final = manifest("complete" if outages == 0 else "incomplete")
    write_report(run_dir / REPORT_FILE, final, ordered)
    write_manifest(manifest_path, final)
    return RunResult(run_dir=run_dir, manifest=final, rows=ordered)
