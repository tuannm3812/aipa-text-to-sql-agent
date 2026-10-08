"""SQL generation via Gemini or Ollama, given a question and a database schema."""

from __future__ import annotations

import os
import re
from typing import Any, cast

from .config import DEFAULT_MODEL_NAME, DEFAULT_OLLAMA_MODEL, DEFAULT_PROVIDER
from .engines import Engine
from .engines.sqlite import SQLiteEngine
from .env import load_env
from .gemini_manager import get_default_gemini_manager

# Placeholders swapped for one engine's prompt fragments by `_assemble_prompt`.
# Each is unique within `_PROMPT_BODY`, so `str.replace` cannot touch anything
# else. `_DIALECT_NAME_PLACEHOLDER` appears twice (the job statement and the
# case-insensitivity rule); `str.replace` substitutes both occurrences with
# the same engine name, which is what both call sites want.
_DIALECT_PLACEHOLDER = "{{DIALECT_SECTION}}"
_DIALECT_NAME_PLACEHOLDER = "{{DIALECT_NAME}}"
_ENGINE_RULES_PLACEHOLDER = "{{ENGINE_RULES_BLOCK}}"

_PROMPT_BODY = """\
You are an expert data analyst and SQL translator.
Your ONLY job is to translate the user's question into a SINGLE {{DIALECT_NAME}} SELECT query.

{{DIALECT_SECTION}}
If the question cannot be answered using the schema, output exactly:
SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error;

If the user requests any data modification (update/insert/delete/drop/alter/create/etc), output exactly:
SELECT 'BLOCKED_UNSAFE_SQL' AS error;

If the user asks for ambigious/ unclear/ not relevant questions and there is no relevant  column in the schema to answer it, output exactly
SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error;

Rules (must follow):
- Output ONLY the SQL query text. No markdown fences, no explanations.
- Use ONLY the tables and columns that exist in the provided schema.
- Generate READ-ONLY SQL: SELECT queries only.
- Do NOT use any data-modifying statements: INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE, TRUNCATE, VACUUM, PRAGMA, ATTACH, DETACH.
- Do NOT repeat or restate the schema/DDL. Never output CREATE TABLE or column lists.
- Output must start with SELECT (or WITH) and contain exactly one query.
{{ENGINE_RULES_BLOCK}}
*** CRITICAL TEXT SEARCHING RULES ***
1. CASE INSENSITIVITY: {{DIALECT_NAME}} '=' is case-sensitive. Whenever you filter by text, you MUST make it case-insensitive. Use `LOWER(column) = LOWER('value')` or `LIKE`.
2. PARTIAL MATCHES: When a user searches for a location, venue, or keyword (e.g., 'bathurst' or 'marine rescue'), assume it is a partial match. ALWAYS use `LIKE '%keyword%'` to search within fields like addresses, names, or descriptions.

ADDITIONAL RULES - DIFFERENCE / DELTA QUESTIONS (must follow):
- The word "difference" can mean either:
  (A) show both groups for comparison, OR
  (B) return a single numeric delta (X - Y).
- If the question asks "What is the difference ..." and does NOT ask to "show/compare both", default to returning a single delta value.
- If the question asks "Do A incur higher than B" or "compare A vs B", return both groups (and optionally the delta).

ADDITONAL RULES - COMPARATIVE QUESTIONS (must follow):
- If the question compares groups/ objects (keywords: "than", "versus", "vs", "compared to", "difference between", "higher/lower than"),
  you MUST return results for ALL compared groups/ objects in one query (not just one side).
- Prefer a single query using CASE buckets + GROUP BY to compute the metric per group.
- If a numeric difference is requested/implicit, you may additionally output (or compute) the difference, but still include both group values.
- Do NOT answer only one cohort unless the user explicitly asks for only that cohort.
"""


def _assemble_prompt(dialect_section: str, dialect_name: str, engine_rules_block: str) -> str:
    """Build the system prompt for one engine's dialect.

    Args:
        dialect_section: The engine's `prompt_dialect_section` - its own
            labelled block (e.g. "SQLITE DIALECT (must follow): ...").
        dialect_name: The engine's `prompt_dialect_name` (e.g. "SQLite"),
            substituted everywhere `_PROMPT_BODY` names the target dialect
            inline, outside the labelled section.
        engine_rules_block: The engine's `prompt_engine_rules_block` - the
            internals/compatibility bullet rules that must name the target
            engine's own internal tables, not another engine's.
    """
    return (
        _PROMPT_BODY.replace(_DIALECT_PLACEHOLDER, dialect_section)
        .replace(_DIALECT_NAME_PLACEHOLDER, dialect_name)
        .replace(_ENGINE_RULES_PLACEHOLDER, engine_rules_block)
    )


# SQLite's assembled prompt, unchanged by the split above: a sha256 test pins
# this exact value because every evaluation figure this project has reported
# was produced under this text.
SQL_TRANSLATION_SYSTEM_PROMPT = _assemble_prompt(
    SQLiteEngine.prompt_dialect_section,
    SQLiteEngine.prompt_dialect_name,
    SQLiteEngine.prompt_engine_rules_block,
)


def _load_gemini_sdk() -> tuple[str, Any, Any | None]:
    try:
        from google import genai
        from google.genai import types

        return "google-genai", genai, types
    except ModuleNotFoundError as e:  # pragma: no cover
        try:
            import google.generativeai as legacy_genai  # type: ignore
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "Gemini support requires google-genai. Run: pip install google-genai"
            ) from e
        return "google-generativeai", legacy_genai, None


def _load_ollama_sdk() -> tuple[Any, Any, Any]:
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_ollama import ChatOllama
    except ModuleNotFoundError as e:  # pragma: no cover
        raise ModuleNotFoundError(
            "Ollama support requires langchain-ollama and langchain-core. "
            "Run: pip install langchain-ollama langchain-core"
        ) from e
    return ChatOllama, HumanMessage, SystemMessage


def _extract_sql_from_text(raw_output: str) -> str:
    raw_output = raw_output.strip()
    blocks = re.findall(
        r"```(?:sql)?\s*(.*?)\s*```",
        raw_output,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        if re.search(r"(?is)^\s*(SELECT|WITH)\b", block):
            return cast(str, block.strip())
    if blocks:
        return cast(str, blocks[0].strip())

    match = re.search(r"(?is)\b(SELECT|WITH)\b.*?;?$", raw_output)
    if match:
        return raw_output[match.start() :].strip()
    return raw_output


def _call_provider(prompt: str, user_prompt: str, *, model_name: str, provider: str) -> str:
    """Send an assembled system prompt and the user prompt to the resolved provider.

    `prompt` is the dialect-aware system prompt from `_assemble_prompt` (SQLite's
    by default, or one from `generate_sql`'s `engine` argument); `user_prompt`
    carries the schema and question. Both Gemini and Ollama branches route through
    here unchanged from their previous inline form in `generate_sql`, so a test can
    assert which system prompt reached the model without patching a vendor SDK.

    Raises:
        ValueError: If `provider` is neither `"gemini"` nor `"ollama"`.
        ModuleNotFoundError: If the SDK required by `provider` is not installed.
    """
    if provider == "gemini":
        sdk_name, genai, genai_types = _load_gemini_sdk()
        key_manager = get_default_gemini_manager()

        def generate_with_key(api_key: str) -> str:
            if sdk_name == "google-genai":
                assert genai_types is not None
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=model_name or DEFAULT_MODEL_NAME,
                    contents=user_prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=512,
                        system_instruction=prompt,
                    ),
                )
            else:
                genai.configure(api_key=api_key)
                model = genai.GenerativeModel(
                    model_name or DEFAULT_MODEL_NAME,
                    system_instruction=prompt,
                )
                response = model.generate_content(
                    contents=user_prompt,
                    generation_config={"temperature": 0.0, "max_output_tokens": 512},
                )
            return _extract_sql_from_text(str(response.text or ""))

        return key_manager.run(generate_with_key)

    if provider == "ollama":
        ChatOllama, HumanMessage, SystemMessage = _load_ollama_sdk()
        model = ChatOllama(
            model=model_name or DEFAULT_OLLAMA_MODEL,
            temperature=0.0,
            num_predict=512,
        )
        response = model.invoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=user_prompt),
            ]
        )
        return _extract_sql_from_text(str(response.content or ""))

    raise ValueError("Unsupported provider. Use 'gemini' or 'ollama'.")


def generate_sql(
    user_question: str,
    schema_text: str,
    *,
    model_name: str = DEFAULT_MODEL_NAME,
    provider: str | None = None,
    engine: Engine | None = None,
) -> str:
    """Call Gemini or Ollama to generate SQL from a question and schema.

    Args:
        user_question: The user's natural-language question.
        schema_text: DDL and hints describing the tables available to query.
        model_name: Provider-specific model identifier.
        provider: `"gemini"` or `"ollama"`. Defaults to the
            `TEXT_TO_SQL_PROVIDER` environment variable, then
            `DEFAULT_PROVIDER`.
        engine: The target database's engine, whose `prompt_dialect_section`
            is assembled into the system prompt so the model is instructed in
            the right dialect. Defaults to SQLite's when omitted.

    Returns:
        The generated SQL text, extracted from the model's raw response.

    Raises:
        ValueError: If the resolved provider is neither `"gemini"` nor
            `"ollama"`.
        ModuleNotFoundError: If the SDK required by the resolved provider is
            not installed.
    """
    load_env()
    selected_provider = (
        (provider or os.environ.get("TEXT_TO_SQL_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    )
    system_prompt = (
        SQL_TRANSLATION_SYSTEM_PROMPT
        if engine is None
        else _assemble_prompt(
            engine.prompt_dialect_section,
            engine.prompt_dialect_name,
            engine.prompt_engine_rules_block,
        )
    )
    schema_header = SQLiteEngine.schema_header if engine is None else engine.schema_header
    user_prompt = f"""\
### {schema_header}
{schema_text}

### User question
{user_question}
"""

    return _call_provider(
        system_prompt, user_prompt, model_name=model_name, provider=selected_provider
    )
