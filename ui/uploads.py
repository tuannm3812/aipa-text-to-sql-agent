"""Writing uploaded files to disk and resolving the active database path."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import streamlit as st
from streamlit.runtime.uploaded_file_manager import UploadedFile

import text_to_sql_agent as backend
from ui.constants import DEMO_DATABASES


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
