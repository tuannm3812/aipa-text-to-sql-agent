"""End-to-end Text-to-SQL orchestration: retrieval, generation, safety, execution."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType

from .config import DEFAULT_MAX_ROWS, DEFAULT_MODEL_NAME, DEFAULT_RAG_TOP_K
from .engines import Engine, open_engine
from .execution import execute_query
from .ingestion import ingest_csvs_to_db
from .llm import generate_sql
from .rag import retrieve_relevant_schema
from .safety import BLOCKED_UNSAFE_SQL, UNANSWERABLE_WITH_GIVEN_SCHEMA, query_refusal
from .schema import get_schema
from .types import QueryResult

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import expressions as exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None


def ask_database(
    question: str,
    *,
    db_path: str = "university_agent.db",
    model_name: str = DEFAULT_MODEL_NAME,
    provider: str | None = None,
    use_rag: bool = True,
    rag_top_k: int = DEFAULT_RAG_TOP_K,
    max_repair_attempts: int = 1,
    work_limit: int | None = None,
    max_rows: int | None = None,
) -> QueryResult:
    """End-to-end Text-to-SQL wrapper: schema retrieval, generation, safety, execution.

    On failure at any stage (generation, safety check, or execution after a
    failed repair attempt), the error is captured in the returned
    `QueryResult.error` rather than raised.

    Args:
        question: The user's natural-language question.
        db_path: Filesystem path to the SQLite database to query.
        model_name: Provider-specific model identifier passed to `generate_sql`.
        provider: `"gemini"` or `"ollama"`; see `generate_sql` for the default.
        use_rag: Whether to retrieve a relevant schema subset via
            `retrieve_relevant_schema` instead of the full schema.
        rag_top_k: Number of schema chunks to retrieve when `use_rag` is set.
        max_repair_attempts: Number of times to ask the model to repair SQL
            that failed execution. `0` disables repair.
        work_limit: The execution budget, in the engine's own unit (SQLite VM
            steps; DuckDB/PostgreSQL milliseconds), passed to `execute_query`
            as `max_vm_steps`. `None` (the default) is the engine's
            `default_work_limit`, as the app has always run; the evaluation
            runner sets it per run so a demo guard cannot decide a
            benchmark's outcome.
        max_rows: Row cap passed to `execute_query`; `None` is
            `DEFAULT_MAX_ROWS`.

    Returns:
        A `QueryResult`. `error` is set to `UNANSWERABLE_WITH_GIVEN_SCHEMA` if
        the model could not answer from the schema, the `query_refusal` code
        (`BLOCKED_UNSAFE_SQL`, or `BLOCKED_UNSUPPORTED_COLUMN_TYPE` for a
        column whose type can run user code) if the generated SQL was
        refused, or the exception text if generation or execution failed.
        A refused *repair* is reported the same way as a refused first
        attempt: its refusal code in `error` and the refused repair in `sql`,
        never the first attempt's stale execution error.

    Raises:
        EngineUnreachableError: If `db_path` cannot be reached by its engine.
            Also a `FileNotFoundError`, for callers relying on that contract.
    """
    engine = open_engine(db_path)
    engine.check_reachable()

    try:
        schema_text = (
            retrieve_relevant_schema(db_path, question, top_k=rag_top_k)
            if use_rag
            else get_schema(db_path)
        )
        sql = generate_sql(
            question, schema_text, model_name=model_name, provider=provider, engine=engine
        )

        refusal = _sentinel_code(sql, engine=engine) or query_refusal(sql, engine=engine)
        if refusal is not None:
            return QueryResult(columns=[], rows=[], sql=sql, error=refusal)
        try:
            return _execute(db_path, sql, work_limit=work_limit, max_rows=max_rows)
        except Exception as e:
            repaired_sql = _repair_sql(
                question,
                schema_text,
                sql,
                f"{type(e).__name__}: {e}",
                model_name=model_name,
                provider=provider,
                max_repair_attempts=max_repair_attempts,
                engine=engine,
            )
            if not repaired_sql:
                raise
            # A refused repair is the terminal verdict: report its code with the
            # SQL it is about, the same contract as a refused first attempt. The
            # first attempt's error would read as repairable when nothing can run.
            repair_refusal = _sentinel_code(repaired_sql, engine=engine) or query_refusal(
                repaired_sql, engine=engine
            )
            if repair_refusal is not None:
                return QueryResult(columns=[], rows=[], sql=repaired_sql, error=repair_refusal)
            return _execute(db_path, repaired_sql, work_limit=work_limit, max_rows=max_rows)
    except Exception as e:
        return QueryResult(columns=[], rows=[], error=f"{type(e).__name__}: {e}")


def ask_database_with_sql(
    question: str,
    *,
    db_path: str = "university_agent.db",
    model_name: str = DEFAULT_MODEL_NAME,
    provider: str | None = None,
    use_rag: bool = True,
    rag_top_k: int = DEFAULT_RAG_TOP_K,
    max_repair_attempts: int = 1,
    work_limit: int | None = None,
    max_rows: int | None = None,
    on_repair_error: Callable[[Exception], None] | None = None,
) -> tuple[str, QueryResult]:
    """Same as `ask_database`, but also returns the generated SQL for UI display.

    Args:
        question: The user's natural-language question.
        db_path: Filesystem path to the SQLite database to query.
        model_name: Provider-specific model identifier passed to `generate_sql`.
        provider: `"gemini"` or `"ollama"`; see `generate_sql` for the default.
        use_rag: Whether to retrieve a relevant schema subset via
            `retrieve_relevant_schema` instead of the full schema.
        rag_top_k: Number of schema chunks to retrieve when `use_rag` is set.
        max_repair_attempts: Number of times to ask the model to repair SQL
            that failed execution. `0` disables repair.
        work_limit: See `ask_database`.
        max_rows: See `ask_database`.
        on_repair_error: Called with the exception when the *repair* call to
            the model raises. The repair failure is still swallowed - the
            result is the first attempt's execution error, as always - so
            this changes nothing for a caller that omits it. The evaluation
            runner passes it to tell a provider outage during repair (a 429
            on the second call) from a model that wrote bad SQL.

    Returns:
        A `(sql, QueryResult)` tuple. `sql` is `""` if generation itself
        failed; otherwise it is the SQL that was attempted (repaired SQL
        replaces the original once a repair succeeds, or once a repair is
        refused - the refusal code is then about that SQL). Once execution
        returns, it is the SQL the engine reports it actually ran
        (`QueryResult.sql`) - identical to the generated text on SQLite and
        DuckDB, and on PostgreSQL the same text with each bare table
        schema-qualified (2026-09-27, `PostgresEngine.execute`), because that
        is what produced the rows shown beside it. See `ask_database` for the
        `QueryResult.error` values used.

    Raises:
        EngineUnreachableError: If `db_path` cannot be reached by its engine.
            Also a `FileNotFoundError`, for callers relying on that contract.
    """
    engine = open_engine(db_path)
    engine.check_reachable()

    schema_text = (
        retrieve_relevant_schema(db_path, question, top_k=rag_top_k)
        if use_rag
        else get_schema(db_path)
    )
    try:
        sql = generate_sql(
            question, schema_text, model_name=model_name, provider=provider, engine=engine
        )
    except Exception as e:
        return "", QueryResult(columns=[], rows=[], error=f"{type(e).__name__}: {e}")

    refusal = _sentinel_code(sql, engine=engine) or query_refusal(sql, engine=engine)
    if refusal is not None:
        return sql, QueryResult(columns=[], rows=[], sql=sql, error=refusal)

    try:
        result = _execute(db_path, sql, work_limit=work_limit, max_rows=max_rows)
        return result.sql or sql, result
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        repaired_sql = _repair_sql(
            question,
            schema_text,
            sql,
            error_text,
            model_name=model_name,
            provider=provider,
            max_repair_attempts=max_repair_attempts,
            engine=engine,
            on_repair_error=on_repair_error,
        )
        if repaired_sql:
            # Same contract as `ask_database`: a refused repair is reported as
            # its own refusal, paired with the refused SQL, never executed.
            repair_refusal = _sentinel_code(repaired_sql, engine=engine) or query_refusal(
                repaired_sql, engine=engine
            )
            if repair_refusal is not None:
                return repaired_sql, QueryResult(
                    columns=[], rows=[], sql=repaired_sql, error=repair_refusal
                )
            try:
                repaired_result = _execute(
                    db_path, repaired_sql, work_limit=work_limit, max_rows=max_rows
                )
                return repaired_result.sql or repaired_sql, repaired_result
            except Exception as repaired_error:
                error_text = f"{type(repaired_error).__name__}: {repaired_error}"
        return sql, QueryResult(columns=[], rows=[], sql=sql, error=error_text)


def ask_from_files(
    question: str,
    file_paths: list[str] | str,
    *,
    output_db_path: str = "dynamic_agent.db",
    model_name: str = DEFAULT_MODEL_NAME,
    provider: str | None = None,
    use_rag: bool = True,
    rag_top_k: int = DEFAULT_RAG_TOP_K,
    max_repair_attempts: int = 1,
) -> QueryResult:
    """Route a `.db` file directly or ingest CSV files before querying.

    Args:
        question: The user's natural-language question.
        file_paths: One `.db` path, or one or more `.csv` paths (mixing
            extensions is not supported).
        output_db_path: Destination path when `file_paths` are CSVs; ignored
            for a `.db` path.
        model_name: Provider-specific model identifier passed to `generate_sql`.
        provider: `"gemini"` or `"ollama"`; see `generate_sql` for the default.
        use_rag: Whether to retrieve a relevant schema subset via
            `retrieve_relevant_schema` instead of the full schema.
        rag_top_k: Number of schema chunks to retrieve when `use_rag` is set.
        max_repair_attempts: Number of times to ask the model to repair SQL
            that failed execution. `0` disables repair.

    Returns:
        The `QueryResult` from `ask_database` against the resolved database.

    Raises:
        ValueError: If `file_paths` is empty, mixes file extensions, contains
            more than one `.db` path, or uses an unsupported extension.
    """
    paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
    if not paths:
        raise ValueError("file_paths must contain at least one path")

    exts = {Path(p).suffix.lower() for p in paths}
    if exts == {".db"}:
        if len(paths) != 1:
            raise ValueError("Provide exactly one .db file path")
        return ask_database(
            question,
            db_path=paths[0],
            model_name=model_name,
            provider=provider,
            use_rag=use_rag,
            rag_top_k=rag_top_k,
            max_repair_attempts=max_repair_attempts,
        )

    if exts == {".csv"}:
        db_path = ingest_csvs_to_db(paths, output_db_path=output_db_path)
        return ask_database(
            question,
            db_path=db_path,
            model_name=model_name,
            provider=provider,
            use_rag=use_rag,
            rag_top_k=rag_top_k,
            max_repair_attempts=max_repair_attempts,
        )

    raise ValueError(f"Unsupported or mixed file types: {sorted(exts)}")


def _sentinel_code(sql: str, *, engine: Engine) -> str | None:
    """The error code for a control statement the model itself emitted, else `None`.

    The system prompt tells the model to answer a data-modification request with
    `SELECT 'BLOCKED_UNSAFE_SQL' AS error;` and an unanswerable question with
    `SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error;`. Both are valid SELECTs, so
    `query_refusal` passes them; checked first, they are reported as the model's own
    verdict instead of being executed as a query that returns the sentinel text.

    Recognised structurally, never by substring: `sql` must parse, in the engine's
    dialect, to exactly one `SELECT` whose only clause is a projection list of a
    single string literal (optionally aliased) equal to one of the two codes. Keyword
    case, whitespace, a trailing semicolon and the alias form do not matter. A
    comment, a column name or a filter literal that merely contains a code is an
    ordinary query, and so is anything with a `FROM`, `WHERE`, `DISTINCT`, set
    operation, CTE or second statement, or that does not parse: all of those go on to
    `query_refusal` unchanged, so a non-read-only statement stays governed by it.
    """
    if sqlglot is None or exp is None:
        return None
    try:
        statements = sqlglot.parse(sql, read=engine.sqlglot_dialect)
    except Exception:
        return None
    if len(statements) != 1:
        return None
    select = statements[0]
    if not isinstance(select, exp.Select):
        return None
    if any(value for key, value in select.args.items() if key != "expressions"):
        return None
    projections = select.args.get("expressions") or []
    if len(projections) != 1:
        return None
    value = projections[0]
    if isinstance(value, exp.Alias):
        value = value.this
    if not (isinstance(value, exp.Literal) and value.is_string):
        return None
    for code in (BLOCKED_UNSAFE_SQL, UNANSWERABLE_WITH_GIVEN_SCHEMA):
        if value.this == code:
            return code
    return None


def _execute(
    db_path: str, sql: str, *, work_limit: int | None, max_rows: int | None
) -> QueryResult:
    """`execute_query` with `None` meaning the default for each budget.

    `max_vm_steps=None` is already the engine's own default; `max_rows=None` maps to
    `DEFAULT_MAX_ROWS`, so a caller passing neither runs exactly as before.
    """
    return execute_query(
        db_path,
        sql,
        max_rows=DEFAULT_MAX_ROWS if max_rows is None else max_rows,
        max_vm_steps=work_limit,
    )


def _repair_sql(
    question: str,
    schema_text: str,
    failed_sql: str,
    error_text: str,
    *,
    model_name: str,
    provider: str | None,
    max_repair_attempts: int,
    engine: Engine,
    on_repair_error: Callable[[Exception], None] | None = None,
) -> str | None:
    if max_repair_attempts < 1:
        return None
    dialect_name = engine.prompt_dialect_name
    repair_question = f"""\
Repair the SQL for the original question.

Original question:
{question}

Failed SQL:
{failed_sql}

{dialect_name} error:
{error_text}

Return only one corrected {dialect_name} SELECT query.
"""
    try:
        return generate_sql(
            repair_question, schema_text, model_name=model_name, provider=provider, engine=engine
        )
    except Exception as exc:
        if on_repair_error is not None:
            on_repair_error(exc)
        return None
