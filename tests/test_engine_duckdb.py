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

from text_to_sql_agent import is_safe_query  # noqa: E402
from text_to_sql_agent.engines import open_engine  # noqa: E402
from text_to_sql_agent.engines.duckdb import DuckDBEngine  # noqa: E402


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


# Every DuckDB table function that reads a file, given a single string-path
# argument. Verified 2026-09-19 that each accepts exactly this shape and
# raises `duckdb.PermissionException` at the connection layer before ever
# opening the path - see the module docstring on why that layer, not
# `is_safe_query`, is what actually stops them.
FILE_READING_FUNCTIONS = [
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_json_objects",
    "read_json_objects_auto",
    "read_ndjson",
    "read_ndjson_auto",
    "read_ndjson_objects",
    "read_parquet",
    "read_duckdb",
    "read_text",
    "read_blob",
    "parquet_scan",
    "glob",
    "sniff_csv",
]


@pytest.mark.parametrize("function_name", FILE_READING_FUNCTIONS)
def test_file_reading_functions_are_blocked_at_both_layers(secret_and_engine, function_name):
    """Pins both layers together: the validator's name-based check (defence in
    depth) and the connection's `enable_external_access=False` (load-bearing).

    A regression that removed either layer alone would still pass the other
    half of this test - only removing both would go unnoticed, which is why
    Step 3a and this test are separate but both required.
    """
    secret, engine = secret_and_engine
    sql = f"SELECT * FROM {function_name}('{secret}')"

    assert not is_safe_query(sql, engine=engine), (
        f"{function_name} must be in DuckDBEngine.internal_prefixes/internal_names"
    )
    with pytest.raises(Exception) as caught:  # noqa: B017
        engine.execute(sql, max_rows=10, work_limit=0)
    assert "hunter2" not in str(caught.value)


# Every distinct table-valued and table-macro function `duckdb_functions()`
# reports in the installed DuckDB version, classified once, here, instead of
# left implicit in `DuckDBEngine.internal_prefixes`/`internal_names`.
#
# The bar for this allowlist is deliberately narrower than "does not touch
# the filesystem" - that was Fix 1's criterion, and it was wrong: it let 5
# administrative functions (`checkpoint`, `enable_profiling`,
# `enable_logging`, `disable_logging`, `truncate_duckdb_logs`) pass
# `is_safe_query` and *execute* against a real `DuckDBEngine`, confirmed by
# probing each directly - low impact today only because `execute` opens a
# fresh connection per call, so any state change dies with it, not because
# anything meant to stop them. The validator's job is least privilege, not
# "provably harmless": allow only what a legitimate analytical question could
# need. Every entry below passed that bar, not just a safety check:
# - `range`, `generate_series`: generate a numeric/date sequence, e.g. to
#   left-join against for a gapless daily/monthly report.
# - `unnest`: expand an array/list column - a common analytical need.
# - `json_each`, `json_tree`: iterate a JSON column's keys/values - useful
#   wherever the target schema stores JSON.
# - `histogram`, `histogram_values`, `summary`: compute distribution/summary
#   statistics over a table or column - directly analytical.
# Administrative functions (`checkpoint`, `force_checkpoint`,
# `enable_logging`/`disable_logging`, `enable_profiling`/`disable_profiling`,
# `truncate_duckdb_logs`), Python-bridge functions (`arrow_scan`,
# `arrow_scan_dumb`, `pandas_scan`, `python_map_function` - these dereference
# a live Python object registered on the connection, which nothing in this
# codebase does, so they are also unreachable in practice), dev/test
# scaffolding (`test_all_types`, `test_vector_types`, `icu_calendar_names`,
# `seq_scan`) and synthetic-data generators with no real analytical use
# (`repeat`, `repeat_row`) are all in `DuckDBEngine.internal_names` instead -
# see that class for the full reasoning on each.
VERIFIED_SAFE_TABLE_FUNCTIONS = frozenset(
    {
        "range",
        "generate_series",
        "unnest",
        "json_each",
        "json_tree",
        "histogram",
        "histogram_values",
        "summary",
    }
)

# The 5 administrative functions Fix 2's review found passing `is_safe_query`
# and actually executing against a real `DuckDBEngine`, under Fix 1's "does
# not touch the filesystem" criterion. Confirmed grep-clean: none of these
# names appear anywhere in text_to_sql_agent/, ui/, scripts/, or
# evaluation/cases.json.
_PREVIOUSLY_EXECUTED_ADMINISTRATIVE_FUNCTIONS = [
    "checkpoint",
    "enable_profiling",
    "enable_logging",
    "disable_logging",
    "truncate_duckdb_logs",
]


@pytest.mark.parametrize("function_name", _PREVIOUSLY_EXECUTED_ADMINISTRATIVE_FUNCTIONS)
def test_administrative_functions_are_refused_by_the_validator(function_name):
    """Regression pin for Fix 2: these 5 passed `is_safe_query` and executed
    before `DuckDBEngine.internal_names` grew an explicit administrative-
    function bucket. A model has no legitimate reason to force a checkpoint
    or toggle logging/profiling, so the validator must refuse them outright,
    not merely fail to find a way to abuse them.
    """
    engine = DuckDBEngine("unused.duckdb")
    assert not is_safe_query(f"SELECT * FROM {function_name}()", engine=engine)


def test_every_duckdb_table_function_is_classified():
    """Sweeps every table/table_macro function this DuckDB build reports and
    requires each to be either blocked by `is_safe_query`'s internals check or
    on the verified allowlist above.

    Classification is checked directly against
    `DuckDBEngine.internal_prefixes`/`internal_names` - the same data
    `is_safe_query`'s `_references_internals` matches a parsed function name
    against - rather than by round-tripping a synthetic `SELECT * FROM
    {name}()` through `is_safe_query` itself. That round trip was tried
    first and rejected: several real, already-classified-safe functions
    (`range`, `generate_series`, `repeat`, ...) take required positional
    arguments, so a zero-arg probe call fails to *parse* in sqlglot's DuckDB
    dialect and `is_safe_query` returns `False` for a reason that has nothing
    to do with the internals check - which would make a future unclassified,
    genuinely unsafe function that happens to *also* require arguments look
    "blocked" here when it was never actually matched by name. Matching the
    prefix/name sets directly has no such false negative.

    This is the regression form of Task 6's Step 5 probe: that probe only
    swept names matching `duckdb%` plus a fixed tail list, which is how
    `sniff_csv` (and, on closer sweep, `query`, `query_table`,
    `json_execute_serialized_sql`, `which_secret`, and the non-`_scan`
    `parquet_*` metadata functions) got missed the first time. Sweeping
    `duckdb_functions()` itself means a function a future DuckDB release adds
    fails this test instead of silently passing `is_safe_query` - forcing
    whoever bumps the `duckdb` pin to classify it one way or the other.
    `test_file_reading_functions_are_blocked_at_both_layers` above still
    proves the full, real `is_safe_query` -> `engine.execute` path for the
    16 functions known to read files.
    """
    engine = DuckDBEngine("unused.duckdb")
    con = duckdb.connect(":memory:")
    try:
        names = {
            row[0]
            for row in con.execute(
                "SELECT function_name FROM duckdb_functions() "
                "WHERE function_type IN ('table', 'table_macro')"
            ).fetchall()
        }
    finally:
        con.close()

    unclassified = []
    for name in sorted(names):
        blocked = name.startswith(engine.internal_prefixes) or name in engine.internal_names
        allowlisted = name in VERIFIED_SAFE_TABLE_FUNCTIONS
        if not blocked and not allowlisted:
            unclassified.append(name)
        # A name cannot be both: an allowlisted function that starts being
        # blocked (e.g. because a broader prefix was added later) should
        # have its allowlist entry removed, not silently pass through it.
        if blocked and allowlisted:
            unclassified.append(f"{name} (blocked AND allowlisted - remove one)")

    assert not unclassified, (
        f"DuckDB added table function(s) with no safety classification: {unclassified}. "
        "Verify whether each touches the filesystem, executes opaque SQL, or exposes "
        "engine internals, then add it to DuckDBEngine.internal_prefixes/internal_names "
        "(if unsafe) or VERIFIED_SAFE_TABLE_FUNCTIONS above (if verified safe)."
    )
