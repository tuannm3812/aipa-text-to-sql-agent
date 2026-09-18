"""Tests for `ui.uploads.redact_dsn`.

The "Connection string" sidebar option must never echo a DSN's password back
to the page - not in the "Using `...`" caption, and not in an exception
message a driver raised that happened to embed the DSN it failed to reach.
No PostgreSQL engine exists yet (Phase 3b), but its DSN form is exactly the
credentials-bearing case this guards, so it is tested here even though
nothing in this repo can construct one today.
"""

from __future__ import annotations

import pytest

from ui.uploads import redact_dsn


def test_redact_dsn_masks_a_postgresql_password() -> None:
    dsn = "postgresql://user:password@host/db"
    assert redact_dsn(dsn) == "postgresql://user:***@host/db"


def test_redact_dsn_does_not_leak_the_password_substring() -> None:
    dsn = "postgresql://alice:hunter2@db.example.com:5432/prod"
    redacted = redact_dsn(dsn)
    assert "hunter2" not in redacted
    assert redacted == "postgresql://alice:***@db.example.com:5432/prod"


def test_redact_dsn_leaves_a_credential_free_dsn_unchanged() -> None:
    assert redact_dsn("sqlite:///path/to.db") == "sqlite:///path/to.db"
    assert redact_dsn("duckdb:///path/to.duckdb") == "duckdb:///path/to.duckdb"


def test_redact_dsn_leaves_plain_text_unchanged() -> None:
    assert redact_dsn("input database not found") == "input database not found"


def test_redact_dsn_masks_a_dsn_embedded_inside_a_longer_error_message() -> None:
    message = "could not connect to postgresql://user:s3cr3t@host:5432/db: timed out"
    redacted = redact_dsn(message)
    assert "s3cr3t" not in redacted
    assert redacted == "could not connect to postgresql://user:***@host:5432/db: timed out"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@host/db",
        "mysql://u:p@host:3306/db",
        "postgres://u:p@127.0.0.1/db",
    ],
)
def test_redact_dsn_masks_credentials_for_any_scheme(dsn: str) -> None:
    assert ":p@" not in redact_dsn(dsn)
    assert ":***@" in redact_dsn(dsn)
