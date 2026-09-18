"""Writing uploaded files to disk and resolving the active database path."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path

import streamlit as st
from streamlit.runtime.uploaded_file_manager import UploadedFile

import text_to_sql_agent as backend
from ui.constants import DEMO_DATABASES

# Matches the credentials portion of a `scheme://user:password@host` DSN.
# `postgresql://user:password@host/db` (Phase 3b) is the motivating case, but
# this matches any scheme so a `sqlite://`/`duckdb://` DSN with embedded
# credentials is caught too, and does nothing to a DSN with none.
_DSN_CREDENTIALS_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<user>[^:@/\s]+):(?P<password>[^@/\s]+)@"
)


def redact_dsn(text: str) -> str:
    """Replace the password in any `scheme://user:password@host` substring with `***`.

    Applied wherever a connection string could appear in something shown to
    the page - a caption for the "Connection string" sidebar field, or an
    exception message a driver raised that happened to echo the DSN it failed
    to reach. `text` need not itself be a bare DSN; only the credentials
    portion of a matching substring is replaced, so passing through an
    arbitrary error message is safe.

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


def write_uploaded_db(uploaded: UploadedFile) -> str:
    """Write an uploaded `.db` file to a temp path and return that path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    Path(path).write_bytes(uploaded.getbuffer())
    return path


def write_uploaded_csvs(uploaded_list: list) -> list[str]:
    """Write uploaded CSV files to a temp directory and return their paths."""
    d = Path(tempfile.mkdtemp(prefix="streamlit_csv_"))
    paths: list[str] = []
    for uf in uploaded_list:
        p = d / uf.name
        p.write_bytes(uf.getbuffer())
        paths.append(str(p))
    return paths


def active_db_path() -> str | None:
    """Resolve DB path from sidebar widgets (with caching for uploads)."""
    source = st.session_state.get("sb_source", "Demo database")

    if source == "Demo database":
        demo_name = st.session_state.get("sb_demo_db", "University")
        demo = DEMO_DATABASES.get(demo_name)
        if not demo:
            return None
        p = Path(demo["path"])
        return str(p.resolve()) if p.is_file() else None

    if source == "Path on disk":
        raw = (st.session_state.get("sb_path_db") or "").strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        return str(p.resolve()) if p.is_file() else None

    if source == "Upload `.db`":
        uf = st.session_state.get("sb_upload_db")
        if uf is None:
            return None
        sig = ("db", uf.name, getattr(uf, "size", 0))
        key = f"_db_upload_{hash(sig)}"
        if key not in st.session_state:
            st.session_state[key] = write_uploaded_db(uf)
        return st.session_state[key]

    if source == "Connection string":
        # Read straight from session state each call rather than caching:
        # the DSN is session-only and must never be written to disk, so
        # there is no on-disk cache key to build the way the upload
        # branches above do.
        dsn = (st.session_state.get("sb_dsn") or "").strip()
        if not dsn:
            return None
        engine = backend.open_engine(dsn)
        engine.check_reachable()
        return dsn

    # CSV(s)
    files = st.session_state.get("sb_upload_csv")
    if not files:
        return None
    csv_sig = tuple(sorted((f.name, getattr(f, "size", 0)) for f in files))
    if st.session_state.get("_csv_sig") != csv_sig:
        try:
            csv_paths = write_uploaded_csvs(list(files))
            out_db = Path(tempfile.gettempdir()) / f"ingested_{uuid.uuid4().hex}.db"
            st.session_state["_csv_db_path"] = backend.ingest_csvs_to_db(csv_paths, str(out_db))
            st.session_state["_csv_sig"] = csv_sig
        except Exception:
            st.session_state["_csv_db_path"] = None
            st.session_state["_csv_sig"] = None
            raise
    return st.session_state.get("_csv_db_path")
