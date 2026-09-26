"""Text-to-SQL agent package: natural language to safe, read-only SQL queries.

Re-exports the public surface used by `app.py` and the evaluation scripts -
configuration defaults, environment loading, schema retrieval, SQL
generation, safety validation, and query execution.
"""

from .config import (
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_VM_STEPS,
    DEFAULT_MODEL_NAME,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_RAG_EMBEDDING_WEIGHT,
    DEFAULT_RAG_NEIGHBORS,
    DEFAULT_RAG_SEMANTIC_WEIGHT,
    DEFAULT_RAG_TOP_K,
    DEFAULT_VALUE_HINT_LIMIT,
    DEFAULT_VALUE_HINT_MAX_CARDINALITY,
    DEFAULT_WORK_LIMIT_MS,
    RAG_SYNONYMS,
)
from .data_setup import create_dummy_university_data, write_university_db
from .dsn import redact_dsn
from .engines import Engine, open_engine
from .env import load_env
from .evaluation import (
    CaseScore,
    canonical_value,
    load_cases,
    normalise_rows,
    rows_match,
    run_gold,
    score_case,
)
from .execution import execute_query
from .ingestion import ingest_csvs_to_db, normalize_table_name
from .llm import SQL_TRANSLATION_SYSTEM_PROMPT, generate_sql
from .pipeline import ask_database, ask_database_with_sql, ask_from_files
from .rag import retrieve_relevant_schema, retrieve_schema_chunks, retrieve_schema_context
from .safety import is_safe_query
from .schema import get_schema, get_schema_chunks
from .types import QueryResult, SchemaChunk, SchemaRetrievalResult

__all__ = [
    "CaseScore",
    "DEFAULT_MAX_ROWS",
    "DEFAULT_MAX_VM_STEPS",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_PROVIDER",
    "DEFAULT_RAG_EMBEDDING_WEIGHT",
    "DEFAULT_RAG_NEIGHBORS",
    "DEFAULT_RAG_SEMANTIC_WEIGHT",
    "DEFAULT_RAG_TOP_K",
    "DEFAULT_VALUE_HINT_LIMIT",
    "DEFAULT_VALUE_HINT_MAX_CARDINALITY",
    "DEFAULT_WORK_LIMIT_MS",
    "Engine",
    "QueryResult",
    "RAG_SYNONYMS",
    "SQL_TRANSLATION_SYSTEM_PROMPT",
    "SchemaChunk",
    "SchemaRetrievalResult",
    "ask_database",
    "ask_database_with_sql",
    "ask_from_files",
    "canonical_value",
    "create_dummy_university_data",
    "execute_query",
    "generate_sql",
    "get_schema",
    "get_schema_chunks",
    "ingest_csvs_to_db",
    "is_safe_query",
    "load_cases",
    "load_env",
    "normalise_rows",
    "normalize_table_name",
    "open_engine",
    "redact_dsn",
    "retrieve_relevant_schema",
    "retrieve_schema_chunks",
    "retrieve_schema_context",
    "rows_match",
    "run_gold",
    "score_case",
    "write_university_db",
]
