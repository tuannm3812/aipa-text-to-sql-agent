from __future__ import annotations

import hashlib
import importlib
from unittest.mock import patch

import pytest

from text_to_sql_agent.engines import _ENGINES
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


_UNUSED_DSNS: dict[str, str] = {
    "sqlite": "unused.db",
    "duckdb": "unused.duckdb",
    "postgres": "postgresql://unused/unused",
}


def _known_engine_classes() -> list[type]:
    """Every `Engine` implementation whose driver is installed, enumerated
    from the scheme registry `open_engine` itself dispatches on.

    Built from `text_to_sql_agent.engines._ENGINES` (Task 2), deduplicating
    by `(module, class)` so `postgresql` and `postgres` - both
    `PostgresEngine` - contribute one entry, not two. An earlier version of
    this function was a hand-maintained list that named `SQLiteEngine` and
    conditionally imported `DuckDBEngine`; a Codex review (2026-09-26) found
    it had already gone stale - `PostgresEngine` existed and was never added
    - while its own docstring falsely claimed it grew automatically. Reading
    `_ENGINES` instead means an engine registered there needs no second edit
    here; `test_known_engine_classes_covers_every_registry_scheme` below
    pins that guarantee independently of this function's own logic.

    A scheme whose driver extra is not installed (e.g. `duckdb`/`postgres`
    without the `engines` extra) is skipped rather than failing collection,
    the same accommodation `pytest.importorskip` makes elsewhere in this
    module.
    """
    classes: list[type] = []
    seen: set[tuple[str, str]] = set()
    for module_name, class_name, _extra in _ENGINES.values():
        key = (module_name, class_name)
        if key in seen:
            continue
        seen.add(key)
        try:
            module = importlib.import_module(module_name, "text_to_sql_agent.engines")
        except ImportError:
            continue
        classes.append(getattr(module, class_name))
    return classes


@pytest.mark.parametrize("engine_cls", _known_engine_classes())
def test_assembled_prompt_names_no_other_known_engine(engine_cls: type) -> None:
    """For every known engine, its assembled system prompt must not name a
    *different* engine's dialect anywhere outside its own labelled
    `prompt_dialect_section` - which is free to name another dialect for
    contrast, as DuckDB's section legitimately does for SQLite.
    """
    engine = engine_cls(_UNUSED_DSNS[engine_cls.name])
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


@pytest.mark.parametrize("scheme", sorted(_ENGINES))
def test_known_engine_classes_covers_every_registry_scheme(scheme: str) -> None:
    """Every scheme `open_engine` can resolve must produce a class in
    `_known_engine_classes()`, so the cross-dialect guard above
    (`test_assembled_prompt_names_no_other_known_engine`) is parametrized
    over every engine `open_engine` itself would route to - not just
    whichever ones someone remembered to add by hand. This is the guard
    Codex's 2026-09-26 review found missing: `PostgresEngine` was already
    registered in `_ENGINES` and already reachable through `open_engine`,
    but absent from the old hand-maintained list, so it never joined the
    no-foreign-dialect check at all.

    Skips a scheme whose driver extra genuinely is not installed - the same
    exemption `_known_engine_classes()` makes - rather than failing a
    machine that never installed the `engines` extra.
    """
    module_name, class_name, extra = _ENGINES[scheme]
    try:
        module = importlib.import_module(module_name, "text_to_sql_agent.engines")
    except ImportError:
        pytest.skip(f"install the {extra!r} extra to cover the {scheme!r} scheme")
    expected_cls = getattr(module, class_name)

    assert expected_cls in _known_engine_classes(), (
        f"{class_name} is registered for scheme {scheme!r} in _ENGINES but is "
        "missing from _known_engine_classes()"
    )
