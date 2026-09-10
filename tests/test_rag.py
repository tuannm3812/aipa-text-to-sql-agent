from __future__ import annotations

import text_to_sql_agent_mvp as agent


def test_retrieve_schema_chunks_selects_relevant_tables(
    customers_sales_courses_db: str,
) -> None:
    chunks = agent.retrieve_schema_chunks(
        customers_sales_courses_db,
        "total sales amount by customer",
        top_k=1,
    )
    names = {chunk.table_name for chunk in chunks}

    assert "sales" in names
    assert "customers" in names
    assert "courses" not in names


def test_schema_rag_expands_business_synonyms(customers_sales_courses_db: str) -> None:
    context = agent.retrieve_schema_context(
        customers_sales_courses_db,
        "revenue by client",
        top_k=1,
    )
    names = {chunk.table_name for chunk in context.chunks}

    assert "sales" in names
    assert "customers" in names
    assert "revenue" in context.expanded_tokens
    assert "customer" in context.expanded_tokens
    assert "Schema RAG strategy" in context.report


def test_retrieve_relevant_schema_returns_only_selected_ddl(customers_courses_db: str) -> None:
    schema = agent.retrieve_relevant_schema(customers_courses_db, "customer names", top_k=1)

    assert "CREATE TABLE customers" in schema
    assert "CREATE TABLE courses" not in schema
