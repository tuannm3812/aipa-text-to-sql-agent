from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from text_to_sql_agent.engines.sqlite import SQLiteEngine
from text_to_sql_agent.llm import (
    SQL_TRANSLATION_SYSTEM_PROMPT,
    _assemble_prompt,
    generate_sql,
)


def test_the_sqlite_prompt_is_unchanged() -> None:
    """Phase 1 protected this string from reformatting for the same reason:
    a changed prompt silently moves every evaluation figure.
    """
    digest = hashlib.sha256(SQL_TRANSLATION_SYSTEM_PROMPT.encode()).hexdigest()
    assert digest == "89d91cbac0ecc8b32b0af647d3d1238f513249cf6cdcc9bf4ff7eb597be333c3"


def test_assemble_prompt_reproduces_the_sqlite_prompt_from_its_section() -> None:
    """`_assemble_prompt` must round-trip on the exact section it was split from."""
    assert _assemble_prompt(SQLiteEngine.prompt_dialect_section) == SQL_TRANSLATION_SYSTEM_PROMPT


def test_assemble_prompt_swaps_only_the_dialect_section() -> None:
    other_section = "OTHER DIALECT (must follow):\n- Not SQLite.\n"
    assembled = _assemble_prompt(other_section)

    assert other_section in assembled
    assert "SQLITE DIALECT" not in assembled
    # Everything outside the dialect section is untouched.
    assert assembled.replace(other_section, SQLiteEngine.prompt_dialect_section) == (
        SQL_TRANSLATION_SYSTEM_PROMPT
    )


def test_generate_sql_defaults_to_the_sqlite_prompt_without_an_engine() -> None:
    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql("how many rows", "CREATE TABLE t (a INTEGER);")

    assert seen["prompt"] == SQL_TRANSLATION_SYSTEM_PROMPT


def test_generate_sql_uses_the_given_engines_dialect_section() -> None:
    class _StandInEngine:
        prompt_dialect_section = "OTHER DIALECT (must follow):\n- Not SQLite.\n"
        schema_header = "Other schema (DDL)"

    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql(
            "how many rows",
            "CREATE TABLE t (a INTEGER);",
            engine=_StandInEngine(),  # type: ignore[arg-type]
        )

    assert _StandInEngine.prompt_dialect_section in seen["prompt"]
    assert "SQLITE DIALECT" not in seen["prompt"]


def test_generate_sql_user_prompt_header_is_sqlite_by_default() -> None:
    """A previous task's reviewer proved the full SDK payload byte-identical to
    before the engine split; a changed SQLite header would break that.
    """
    seen: dict[str, str] = {}

    def fake_call(_prompt: str, user_prompt: str, *_a: object, **_k: object) -> str:
        seen["user_prompt"] = user_prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql("how many rows", "CREATE TABLE t (a INTEGER);")

    assert "### SQLite schema (DDL)" in seen["user_prompt"]


def test_generate_sql_user_prompt_header_is_engine_aware_for_duckdb() -> None:
    pytest.importorskip("duckdb", reason="install the duckdb extra")
    from text_to_sql_agent.engines.duckdb import DuckDBEngine

    seen: dict[str, str] = {}

    def fake_call(_prompt: str, user_prompt: str, *_a: object, **_k: object) -> str:
        seen["user_prompt"] = user_prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql(
            "how many rows",
            "CREATE TABLE t (a INTEGER);",
            engine=DuckDBEngine("unused.duckdb"),
        )

    assert "### DuckDB schema (DDL)" in seen["user_prompt"]
    assert "### SQLite schema (DDL)" not in seen["user_prompt"]


def test_call_provider_rejects_an_unsupported_provider() -> None:
    from text_to_sql_agent.llm import _call_provider

    with pytest.raises(ValueError, match="Unsupported provider"):
        _call_provider("system", "user", model_name="m", provider="not-a-real-provider")
