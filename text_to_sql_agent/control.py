"""Recognising the model's two control statements, and trimming prose that trails one.

The system prompt tells the model to answer a data-modification request with
`SELECT 'BLOCKED_UNSAFE_SQL' AS error;` and an unanswerable question with
`SELECT 'UNANSWERABLE_WITH_GIVEN_SCHEMA' AS error;`. This module is the one place that
decides what counts as such a statement. It sits below both `llm` (whose extractor trims
trailing prose) and `pipeline` (which reports the verdict), so neither imports the other.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import ModuleType

from .safety import BLOCKED_UNSAFE_SQL, UNANSWERABLE_WITH_GIVEN_SCHEMA

sqlglot: ModuleType | None
exp: ModuleType | None
try:
    import sqlglot
    from sqlglot import expressions as exp
except ModuleNotFoundError:  # pragma: no cover
    sqlglot = None
    exp = None

_DEFAULT_DIALECT = "sqlite"


def control_statement_code(sql: str, *, dialect: str) -> str | None:
    """The error code if `sql` is exactly one of the model's control statements, else `None`.

    Recognised structurally, never by substring: `sql` must parse, in `dialect`, to
    exactly one `SELECT` whose only clause is a projection list of a single string
    literal (optionally aliased) equal to one of the two codes. Keyword case,
    whitespace, a trailing semicolon and the alias form do not matter. A comment, a
    column name or a filter literal that merely contains a code is an ordinary query,
    and so is anything with a `FROM`, `WHERE`, `DISTINCT`, set operation, CTE or second
    statement, or that does not parse.
    """
    if sqlglot is None or exp is None:
        return None
    try:
        statements = sqlglot.parse(sql, read=dialect)
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


def _looks_like_sql(text: str, *, dialect: str) -> bool:
    """Whether `text` could be SQL, deciding every doubt towards "yes".

    Wrongly calling prose SQL only leaves today's behaviour (the validator refuses the
    whole string), while wrongly calling SQL prose would hide a statement from the
    validator. So it is SQL when sqlglot parses it to anything but a bare expression
    (a bare word or an alias is what a sentence of prose becomes when it parses at
    all), and also when it does not parse but opens with a word sqlglot itself treats
    as the start of a statement or command.
    """
    if sqlglot is None or exp is None:
        return True
    try:
        with _quiet_sqlglot():
            statements = sqlglot.parse(text, read=dialect)
    except Exception:
        return _opens_a_statement(text)
    return any(
        statement is not None and not isinstance(statement, (exp.Condition, exp.Alias))
        for statement in statements
    )


@contextmanager
def _quiet_sqlglot() -> Iterator[None]:
    """Silence sqlglot's "falling back to parsing as a Command" warning while parsing."""
    logger = logging.getLogger("sqlglot")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(level)


def _opens_a_statement(text: str) -> bool:
    assert sqlglot is not None
    from sqlglot import Parser, Tokenizer, TokenType

    try:
        tokens = [t for t in sqlglot.tokenize(text, read=_DEFAULT_DIALECT) if t.text != ";"]
    except Exception:
        # Unbalanced quotes (an apostrophe in a sentence) defeat the tokenizer; fall back to
        # the first word, which is all the keyword check needs.
        word = re.match(r"[\W_]*([A-Za-z]+)", text)
        tokens = sqlglot.tokenize(word.group(1), read=_DEFAULT_DIALECT) if word else []
    if not tokens:
        return False
    token = tokens[0]
    return (
        token.token_type in Parser.STATEMENT_PARSERS
        or token.token_type in (TokenType.SELECT, TokenType.WITH, TokenType.VALUES)
        or token.text.upper() in Tokenizer.COMMANDS
    )


def trim_prose_after_control(text: str, *, dialect: str = _DEFAULT_DIALECT) -> str:
    """Drop prose that follows a leading control statement; otherwise return `text` as is.

    Only when `text` begins with a control statement terminated by its first `;`, and
    what follows is not SQL, is the text cut after that `;`. It is never cut at a `;`
    in general: an ordinary query, or a control statement followed by anything that
    could be SQL (`SELECT 'BLOCKED_UNSAFE_SQL' AS error; DROP TABLE t`), is returned
    unchanged so the validator sees it exactly as the model wrote it. The remainder
    that is dropped is never executed either way, because a control statement is
    reported, not run.
    """
    end = text.find(";")
    if end < 0:
        return text
    head, rest = text[: end + 1], text[end + 1 :]
    if not rest.strip() or control_statement_code(head, dialect=dialect) is None:
        return text
    if _looks_like_sql(rest, dialect=dialect):
        return text
    return head.strip()
