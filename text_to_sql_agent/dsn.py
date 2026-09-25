"""Masking credentials embedded in a database connection string."""

from __future__ import annotations

import re

# Matches the credentials portion of a `scheme://user:password@host` DSN.
# `postgresql://user:password@host/db` (Phase 3b) is the motivating case, but
# this matches any scheme so a `sqlite://`/`duckdb://` DSN with embedded
# credentials is caught too, and does nothing to a DSN with none. The user may
# be empty (`postgresql://:pw@host`), and the password runs greedily to the
# *last* `@` before whitespace, so a pasted, unescaped `/` or `@` inside it is
# masked rather than split. Over-masking a credential-free DSN is the
# acceptable failure; leaking part of a password is not.
_DSN_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^:@/\s]*):(?P<password>\S*)@"
)


def redact_dsn(text: str) -> str:
    """Replace the password in any `scheme://user:password@host` substring with `***`.

    Applied wherever a connection string could appear in something shown to a
    user or written to disk - a caption for the "Connection string" sidebar
    field, an exception message a driver raised that happened to echo the DSN
    it failed to reach, or an evaluation row recorded to a results CSV.
    `text` need not itself be a bare DSN; only the credentials portion of a
    matching substring is replaced, so passing through an arbitrary error
    message is safe.

    No PostgreSQL engine exists yet, but its DSN form
    (`postgresql://user:password@host/db`) is exactly what this guards
    against, so Phase 3b inherits working redaction instead of adding it
    under time pressure.

    Args:
        text: Text that may contain a DSN with embedded credentials.

    Returns:
        `text` with any embedded password replaced by `***`.
    """
    return _DSN_CREDENTIALS_RE.sub(lambda m: f"{m.group('scheme')}{m.group('user')}:***@", text)
