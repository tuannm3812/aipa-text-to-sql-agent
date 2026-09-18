"""DuckDB-specific safety tests: the connection-level filesystem-access guard.

`read_only=True` on a DuckDB connection protects the database *file*, not the
filesystem: a read-only connection can still run `read_csv`, a bare quoted
path (which has no function name for `is_safe_query` to catch), `glob`, and
`COPY ... TO`. These go in their own module, engine-specific rather than in
`test_engine_conformance.py`, for the same reason SQLite's authorizer tests
live in `test_execution.py` rather than the conformance suite: the mechanism
under test - `enable_external_access=False` - has no equivalent on other
engines.

Each test calls `engine.execute` directly, bypassing `is_safe_query` entirely,
because the point is to prove the *connection* refuses these regardless of
what any validator in front of it does or doesn't catch.
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb", reason="install the duckdb extra")

from text_to_sql_agent.engines import open_engine  # noqa: E402


@pytest.fixture
def secret_and_engine(tmp_path):
    secret = tmp_path / "secret.csv"
    secret.write_text("k,v\napi_key,hunter2\n", encoding="utf-8")
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (a INTEGER)")
    con.close()
    return secret, open_engine(f"duckdb://{db}")


@pytest.mark.parametrize(
    "template",
    [
        "SELECT * FROM read_csv('{secret}')",
        "SELECT * FROM '{secret}'",
        "SELECT * FROM glob('{parent}/*')",
    ],
)
def test_filesystem_reads_are_refused_by_the_connection(secret_and_engine, template):
    secret, engine = secret_and_engine
    sql = template.format(secret=secret, parent=secret.parent)
    with pytest.raises(Exception) as caught:  # noqa: B017
        engine.execute(sql, max_rows=10, work_limit=0)
    assert "hunter2" not in str(caught.value)


def test_copy_to_a_file_is_refused_by_the_connection(secret_and_engine, tmp_path):
    _, engine = secret_and_engine
    target = tmp_path / "exfil.csv"
    with pytest.raises(Exception):  # noqa: B017
        engine.execute(f"COPY (SELECT 1) TO '{target}'", max_rows=10, work_limit=0)
    assert not target.exists(), "nothing may be written to disk"
