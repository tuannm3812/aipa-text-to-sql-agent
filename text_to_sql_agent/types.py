"""Dataclasses shared across the agent package: query results and schema retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryResult:
    """The outcome of executing (or attempting to execute) a SQL query.

    Attributes:
        columns: Result-set column names, in order. Empty if no rows.
        rows: Result-set rows, each a tuple of column values.
        sql: The SQL text that was executed, when execution was attempted.
        error: A machine-readable error code or message, or `None` on
            success. See `execute_query` and `text_to_sql_agent.pipeline` for
            the specific values used.
    """

    columns: list[str]
    rows: list[tuple[Any, ...]]
    sql: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the query completed without an error."""
        return self.error is None


@dataclass(frozen=True)
class SchemaChunk:
    """A single table's schema, plus retrieval metadata for RAG scoring.

    Attributes:
        table_name: The table's name.
        ddl: Its `CREATE TABLE` statement.
        columns: Column names.
        foreign_tables: Names of tables referenced by foreign keys.
        search_text: Concatenated text used for lexical/semantic scoring.
        value_hints: Sample distinct values per low-cardinality text column.
        score: Relevance score assigned during retrieval; `0.0` until scored.
        matched_terms: Query terms that matched this chunk, once scored.
        match_reasons: Human-readable explanations of why this chunk matched.
    """

    table_name: str
    ddl: str
    columns: list[str]
    foreign_tables: list[str]
    search_text: str
    value_hints: dict[str, list[str]] | None = None
    score: float = 0.0
    matched_terms: list[str] | None = None
    match_reasons: list[str] | None = None


@dataclass(frozen=True)
class SchemaRetrievalResult:
    """The chunks retrieved for a question, plus the terms and stats behind them.

    Attributes:
        chunks: The selected `SchemaChunk`s, most relevant first.
        query_tokens: Tokens extracted from the question.
        expanded_tokens: `query_tokens` plus synonym expansions.
        top_k: The requested chunk limit that produced this result.
        decomposed_terms: The question broken into aggregations, filters,
            and comparisons, as returned by `decompose_question`.
        full_schema_chars: Character count of the entire schema's prompt text.
        retrieved_schema_chars: Character count of the retrieved chunks' text.
        cache_hit: Whether the underlying schema-chunk cache was hit.
        strategy: Name of the retrieval strategy that produced this result.
    """

    chunks: list[SchemaChunk]
    query_tokens: list[str]
    expanded_tokens: list[str]
    top_k: int
    decomposed_terms: dict[str, list[str]] | None = None
    full_schema_chars: int = 0
    retrieved_schema_chars: int = 0
    cache_hit: bool = False
    strategy: str = "hybrid-bm25-embedding-semantic-values-graph"

    @property
    def schema_text(self) -> str:
        """DDL and value hints for the retrieved chunks, formatted for a prompt."""
        parts: list[str] = []
        for chunk in self.chunks:
            parts.append(chunk.ddl)
            if chunk.value_hints:
                parts.append(
                    "\n".join(
                        f"-- {column} sample values: {', '.join(values)}"
                        for column, values in sorted(chunk.value_hints.items())
                    )
                )
        return "\n\n".join(parts)

    @property
    def prompt_savings_pct(self) -> float:
        """Percentage reduction in prompt schema size vs. the full schema."""
        if self.full_schema_chars <= 0:
            return 0.0
        saved = max(0, self.full_schema_chars - self.retrieved_schema_chars)
        return round((saved / self.full_schema_chars) * 100, 1)

    @property
    def report(self) -> str:
        """A human-readable summary of the retrieval: strategy, terms, and tables."""
        if not self.chunks:
            return "No schema chunks were retrieved."
        lines = [
            f"Schema RAG strategy: {self.strategy}",
            f"Query tokens: {', '.join(self.query_tokens) or '(none)'}",
            f"Expanded tokens: {', '.join(self.expanded_tokens) or '(none)'}",
            f"Prompt schema chars: {self.retrieved_schema_chars}/{self.full_schema_chars} "
            f"saved={self.prompt_savings_pct}%",
            f"Cache hit: {self.cache_hit}",
            "",
            "Query decomposition:",
        ]
        for key, values in (self.decomposed_terms or {}).items():
            lines.append(f"- {key}: {', '.join(values) or '(none)'}")
        lines += [
            "",
            "Retrieved tables:",
        ]
        for chunk in self.chunks:
            terms = ", ".join(chunk.matched_terms or []) or "fallback"
            reasons = "; ".join(chunk.match_reasons or [])
            lines.append(f"- {chunk.table_name} (score={chunk.score:.2f}; terms={terms})")
            if reasons:
                lines.append(f"  {reasons}")
            if chunk.value_hints:
                for column, values in sorted(chunk.value_hints.items()):
                    lines.append(f"  value hints {column}: {', '.join(values)}")
        return "\n".join(lines)
