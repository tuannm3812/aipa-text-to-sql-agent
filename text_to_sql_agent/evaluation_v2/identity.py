"""Run identity: the immutable payload that names a v2 result directory (spec §4.4).

The identity payload holds exactly the fields that define *what is being measured*. Mutable
execution metadata - start time, duration, outage count, status - is deliberately outside it,
so finishing or resuming a run never changes its directory name, and a resume can refuse to
mix results from different code, prompt, suite, database contents or settings by comparing
payloads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Paths under these prefixes never make a run dirty: `.superpowers/` is the subagent scratch
# directory (gitignored, but listed here too so a stray `git add -f` cannot flip `citable`).
_DIRTY_EXEMPT_PREFIXES = (".superpowers/",)

_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class IdentityPayload:
    """The spec's immutable identity fields, in the spec's order.

    ``subset`` is the subset's name or ``"full"``; ``subset_sha256`` is ``""`` for a full run.
    ``database_fingerprint`` is ``database_fingerprint(...)`` over the databases the selected
    cases query: the bytes actually read, which the suite hash and the source archive's
    hash do not pin (an extracted benchmark database is gitignored and can change in place).
    ``retry_policy`` is ``retry_policy(max_retries, retry_base_seconds)``.
    """

    commit: str
    dirty: bool
    suite: str
    suite_sha256: str
    subset: str
    subset_sha256: str
    source_release: str
    database_fingerprint: tuple[tuple[str, str], ...]
    adapter_version: str
    scorer_version: str
    prompt_sha256: str
    provider: str
    model: str
    evidence: bool
    use_rag: bool
    rag_top_k: int
    work_limit: int
    max_rows: int
    max_repair_attempts: int
    retry_policy: str


IDENTITY_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclasses.fields(IdentityPayload))


def canonical_json(value: Any) -> str:
    """The one JSON spelling hashed anywhere in a run: sorted keys, no whitespace, ASCII."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def identity_hash(payload: IdentityPayload) -> str:
    """SHA-256 (lowercase hex) of the payload's canonical JSON."""
    return hashlib.sha256(canonical_json(dataclasses.asdict(payload)).encode()).hexdigest()


def identity_diff(saved: Mapping[str, Any], current: IdentityPayload) -> list[str]:
    """Names of the identity fields where ``saved`` differs from ``current``, sorted.

    A field missing from ``saved`` counts as different, so a manifest written by an older
    runner with fewer identity fields can never be silently resumed. Values are compared in
    their canonical JSON spelling, because a saved manifest is JSON: the fingerprint's tuples
    come back as lists and must still compare equal.
    """
    now = dataclasses.asdict(current)
    return sorted(
        name
        for name in IDENTITY_FIELDS
        if name not in saved or canonical_json(saved[name]) != canonical_json(now[name])
    )


def fingerprint_from_json(value: Any) -> tuple[tuple[str, str], ...]:
    """A ``database_fingerprint`` read back from JSON (a list of ``[path, sha256]`` lists)."""
    return tuple((str(path), str(digest)) for path, digest in value)


def database_fingerprint(
    db_paths: Iterable[str], *, root: Path = REPO_ROOT
) -> tuple[tuple[str, str], ...]:
    """``(path, sha256)`` for each distinct database file, sorted by path.

    ``path`` is relative to ``root`` (POSIX separators) when the file is under it - so two
    checkouts holding identical copies get the same fingerprint - and absolute otherwise. A
    relative ``db_path`` is resolved against ``root``. Each file is streamed through SHA-256
    (``hashlib.file_digest``), never read whole: BIRD dev's databases total about 1.4 GB.

    Raises:
        ValueError: If a ``db_path`` is a ``scheme://`` DSN. A server database has no file to
            hash, and an identity that silently skipped it would bind nothing.
        OSError: If a file cannot be read.
    """
    digests: dict[str, str] = {}
    for db_path in db_paths:
        if "://" in db_path:
            scheme = db_path.split("://", 1)[0]
            raise ValueError(
                f"cannot fingerprint a '{scheme}://' database: the run identity binds each "
                "database file's contents, and only file-backed SQLite databases are supported"
            )
        path = Path(db_path)
        full = path if path.is_absolute() else root / path
        try:
            key = full.relative_to(root).as_posix()
        except ValueError:
            key = full.as_posix()
        if key in digests:
            continue
        with full.open("rb") as handle:
            digests[key] = hashlib.file_digest(handle, "sha256").hexdigest()
    return tuple(sorted(digests.items()))


def retry_policy(max_retries: int, retry_base_seconds: float) -> str:
    """The manifest's retry-policy string. ``float()`` so ``20`` and ``20.0`` agree."""
    return f"{max_retries}x{float(retry_base_seconds)}"


def sanitise(name: str) -> str:
    """Make ``name`` safe as part of one path component.

    ``/`` and ``:`` become ``-`` (the spec's rule, so ``ollama/llama3:latest`` reads
    ``ollama-llama3-latest``), and so does any other character outside ``[A-Za-z0-9._-]`` -
    replaced rather than dropped, so two distinct names cannot collapse into one.
    """
    return _UNSAFE_NAME_CHARS.sub("-", name)


def config_label(*, use_rag: bool, rag_top_k: int, evidence: bool) -> str:
    """The directory name's ``<config>`` part: the settings that change outcomes."""
    rag = f"rag-on-k{rag_top_k}" if use_rag else "rag-off"
    return f"{rag}-evidence-{'on' if evidence else 'off'}"


def run_dir_name(payload: IdentityPayload, *, started: datetime, config: str, nonce: str) -> str:
    """``<YYYY-MM-DDTHHMMSS>_<suite>_<subset>_<provider>_<model>_<config>_<identity8>_<nonce4>``."""
    parts = (
        started.strftime("%Y-%m-%dT%H%M%S"),
        payload.suite,
        payload.subset,
        payload.provider,
        payload.model,
        config,
        identity_hash(payload)[:8],
        nonce,
    )
    return "_".join(sanitise(part) for part in parts)


def allocate_run_dir(
    root: Path, payload: IdentityPayload, *, started: datetime, config: str
) -> Path:
    """Create and return a fresh run directory under ``root``.

    ``os.mkdir`` is exclusive: it fails if the path exists, so an existing directory - a
    finished run, or one another process allocated in the same second - is never reused.
    On a collision a new nonce is drawn until a directory is created.
    """
    root.mkdir(parents=True, exist_ok=True)
    while True:
        nonce = os.urandom(2).hex()
        path = root / run_dir_name(payload, started=started, config=config, nonce=nonce)
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path


def git_state(repo: Path = REPO_ROOT) -> tuple[str, bool]:
    """The checkout's ``HEAD`` commit and whether any tracked file differs from it.

    Untracked files are ignored (``--untracked-files=no``), so a run's own result directory
    under ``evaluation/results/`` does not make the next run dirty; gitignored files never
    appear in ``git status``. When git cannot answer, returns ``("unknown", True)`` - a run
    whose code cannot be pinned is recorded as not citable rather than refused.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    # Porcelain v1 lines are "XY <path>"; the path starts at column 3.
    changed = [line[3:] for line in status.splitlines() if line.strip()]
    dirty = any(not path.startswith(_DIRTY_EXEMPT_PREFIXES) for path in changed)
    return commit, dirty
