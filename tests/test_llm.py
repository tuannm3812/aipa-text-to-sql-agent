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
    """`_assemble_prompt` must round-trip on the exact fragments it was split from."""
    assert (
        _assemble_prompt(
            SQLiteEngine.prompt_dialect_section,
            SQLiteEngine.prompt_dialect_name,
            SQLiteEngine.prompt_engine_rules_block,
        )
        == SQL_TRANSLATION_SYSTEM_PROMPT
    )


def test_assemble_prompt_swaps_only_the_dialect_section() -> None:
    other_section = "OTHER DIALECT (must follow):\n- Not SQLite.\n"
    assembled = _assemble_prompt(
        other_section,
        SQLiteEngine.prompt_dialect_name,
        SQLiteEngine.prompt_engine_rules_block,
    )

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
        prompt_dialect_name = "Other"
        prompt_engine_rules_block = (
            "- Do NOT reference any internal Other tables.\n"
            "- Prefer simple SQL compatible with Other.\n"
        )
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


def test_generate_sql_system_prompt_for_duckdb_has_no_sqlite_instructions() -> None:
    """Phase 3a review (2026-09-21): `_PROMPT_BODY`, the supposedly shared half
    of the prompt, said "SQLite" in four places outside the `{{DIALECT_SECTION}}`
    placeholder - the job statement, the internal-tables rule, the
    "prefer... compatible" rule, and the case-insensitivity rule - so every
    DuckDB generation was told to write SQLite. This covers all four leaked
    lines; `test_generate_sql_uses_the_given_engines_dialect_section` above
    only ever checked the `SQLITE DIALECT` heading was absent, which none of
    these four lines are.
    """
    pytest.importorskip("duckdb", reason="install the duckdb extra")
    from text_to_sql_agent.engines.duckdb import DuckDBEngine

    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql(
            "how many rows",
            "CREATE TABLE t (a INTEGER);",
            engine=DuckDBEngine("unused.duckdb"),
        )

    prompt = seen["prompt"]
    assert "a SINGLE SQLite SELECT query" not in prompt
    assert "internal SQLite tables" not in prompt
    assert "compatible with SQLite" not in prompt
    assert "SQLite '=' is case-sensitive" not in prompt
    # The one legitimate SQLite mention: DuckDB's own dialect section
    # contrasting itself against SQLite's date functions.
    assert "SQLite's strftime/date functions" in prompt


def _known_engine_classes() -> list[type]:
    """Every `Engine` implementation this test suite can construct today.

    Grows automatically as new engines (PostgreSQL, Phase 3b) are added,
    which is the point of `test_assembled_prompt_names_no_other_known_engine`
    below: it is written over this registry, not over four hardcoded
    strings, so a future engine that reintroduces the same leak fails a test
    that already exists rather than needing a new one written for it.
    """
    engines: list[type] = [SQLiteEngine]
    try:
        from text_to_sql_agent.engines.duckdb import DuckDBEngine
    except ModuleNotFoundError:
        return engines
    engines.append(DuckDBEngine)
    return engines


@pytest.mark.parametrize("engine_cls", _known_engine_classes())
def test_assembled_prompt_names_no_other_known_engine(engine_cls: type) -> None:
    """For every known engine, its assembled system prompt must not name a
    *different* engine's dialect anywhere outside its own labelled
    `prompt_dialect_section` - which is free to name another dialect for
    contrast, as DuckDB's section legitimately does for SQLite.
    """
    dsn = "unused.db" if engine_cls is SQLiteEngine else "unused.duckdb"
    engine = engine_cls(dsn)
    seen: dict[str, str] = {}

    def fake_call(prompt: str, *_a: object, **_k: object) -> str:
        seen["prompt"] = prompt
        return "SELECT 1"

    with patch("text_to_sql_agent.llm._call_provider", side_effect=fake_call):
        generate_sql("how many rows", "CREATE TABLE t (a INTEGER);", engine=engine)

    outside_own_section = seen["prompt"].replace(engine.prompt_dialect_section, "")
    for other_cls in _known_engine_classes():
        if other_cls is engine_cls:
            continue
        assert other_cls.prompt_dialect_name not in outside_own_section, (
            f"{engine_cls.name}'s prompt names {other_cls.prompt_dialect_name!r} "
            "outside its own dialect section"
        )
