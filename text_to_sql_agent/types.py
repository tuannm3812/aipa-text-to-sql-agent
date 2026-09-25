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
        table_name: The table's name, bare.
        ddl: Its `CREATE TABLE` statement.
        columns: Column names.
        foreign_tables: Names of tables referenced by foreign keys, spelled
            the same way `qualified_name` spells this one - bare for a table
            in the engine's default schema, `schema.table` otherwise - so a
            foreign-key edge and a chunk's own identity are the same kind of
            key (`rag.py`'s neighbour graph joins on exactly that).
        search_text: Concatenated text used for lexical/semantic scoring.
        schema_name: The schema this table lives in, **only when that is not
            the engine's default schema**; `""` otherwise. Empty is therefore
            both "the default schema" and "no schema recorded", which is what
            lets it default and keeps every pre-Phase-3b construction (and
            `rag.py`, Phase 4's file) correct unchanged. The engine that built
            the chunk is the one that knows its own `default_schema`, so it
            resolves the pair before setting this rather than storing the
            default alongside every chunk.
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
    schema_name: str = ""
    value_hints: dict[str, list[str]] | None = None
    score: float = 0.0
    matched_terms: list[str] | None = None
    match_reasons: list[str] | None = None

    @property
    def qualified_name(self) -> str:
        """`schema.table` outside the engine's default schema, else the bare name.

        This is exactly the spelling that is valid to write in SQL against the
        engine this chunk came from, and exactly the spelling that engine's
        `table_names()` advertises to `safety.is_safe_query` - one identity,
        not one per layer, which is what the 2026-09-25 decision
        (`docs/3_decisions.md`) found the three layers disagreeing about.
        """
        return f"{self.schema_name}.{self.table_name}" if self.schema_name else self.table_name


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
            lines.append(f"- {chunk.qualified_name} (score={chunk.score:.2f}; terms={terms})")
            if reasons:
                lines.append(f"  {reasons}")
            if chunk.value_hints:
                for column, values in sorted(chunk.value_hints.items()):
                    lines.append(f"  value hints {column}: {', '.join(values)}")
        return "\n".join(lines)
