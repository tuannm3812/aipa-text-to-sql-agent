"""The run manifest: identity, provenance and execution metadata in one JSON file (spec §4.4).

``manifest.json`` is written ``incomplete`` before a run's first case and rewritten when the
run ends. Its keys, in order: the identity payload's fields, ``identity_sha256``, ``run_id``,
``mode``, ``case_count``, the ``source`` block (verbatim from the suite's ``.source.json``),
``started``, ``duration_s``, ``outage_count``, ``status``, ``citable``, ``python``,
``packages``, and ``manifest_sha256`` - a checksum of the whole manifest computed with that
one field blank, separate from the identity hash.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from text_to_sql_agent.evaluation_v2.identity import (
    IDENTITY_FIELDS,
    IdentityPayload,
    canonical_json,
    identity_hash,
)

Status = Literal["complete", "incomplete"]

# The runtime dependencies whose versions can move a result: the project's direct
# dependencies plus the optional engine drivers. "not installed" when absent.
RECORDED_PACKAGES: tuple[str, ...] = (
    "duckdb",
    "google-genai",
    "langchain-core",
    "langchain-ollama",
    "pandas",
    "psycopg",
    "python-dotenv",
    "sqlglot",
    "streamlit",
)

# Keys each kind of `.source.json` must carry (spec §4.4). Extra keys are kept, never
# filtered: BIRD's records `questions_url`, `questions_sha256` and more beyond these.
_REQUIRED_SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "download": ("release", "url", "sha256", "licence", "adapter_version"),
    "authored": ("author", "licence"),
}

NOT_APPLICABLE = "n/a"


@dataclass(frozen=True)
class Manifest:
    """Everything recorded about one run. ``to_dict`` is the on-disk form."""

    identity: IdentityPayload
    run_id: str
    mode: str
    case_count: int
    source: dict[str, Any]
    started: str
    duration_s: float
    outage_count: int
    status: Status
    python: str
    packages: dict[str, str]

    @property
    def citable(self) -> bool:
        """Only a run of committed code that finished every case may be cited."""
        return not self.identity.dirty and self.status == "complete"

    def to_dict(self) -> dict[str, Any]:
        """The manifest as written, ``manifest_sha256`` included."""
        data: dict[str, Any] = dataclasses.asdict(self.identity)
        data.update(
            identity_sha256=identity_hash(self.identity),
            run_id=self.run_id,
            mode=self.mode,
            case_count=self.case_count,
            source=self.source,
            started=self.started,
            duration_s=self.duration_s,
            outage_count=self.outage_count,
            status=self.status,
            citable=self.citable,
            python=self.python,
            packages=self.packages,
            manifest_sha256="",
        )
        data["manifest_sha256"] = manifest_sha256(data)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        """Rebuild a manifest from its ``to_dict`` form (derived fields are recomputed)."""
        identity = IdentityPayload(**{name: data[name] for name in IDENTITY_FIELDS})
        return cls(
            identity=identity,
            run_id=data["run_id"],
            mode=data["mode"],
            case_count=data["case_count"],
            source=data["source"],
            started=data["started"],
            duration_s=data["duration_s"],
            outage_count=data["outage_count"],
            status=data["status"],
            python=data["python"],
            packages=data["packages"],
        )


def manifest_sha256(data: dict[str, Any]) -> str:
    """SHA-256 of the manifest's canonical JSON with ``manifest_sha256`` set to ``""``."""
    return hashlib.sha256(canonical_json({**data, "manifest_sha256": ""}).encode()).hexdigest()


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a sibling temp file and ``os.replace``.

    A reader - or a resume after a crash - sees either the old file or the new one, never a
    half-written one.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Write ``manifest`` to ``path`` atomically, as indented JSON in key order."""
    write_atomic(path, json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n")


def read_manifest(path: Path) -> dict[str, Any]:
    """Read a manifest file as a plain dict. Raises ``OSError``/``ValueError`` on failure."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: manifest is not a JSON object")
    return data


def source_path_for(suite_path: Path) -> Path:
    """``evaluation/suites/<suite>.jsonl`` -> ``evaluation/suites/<suite>.source.json``."""
    return suite_path.with_name(f"{suite_path.stem}.source.json")


def load_source(suite_path: Path) -> dict[str, Any]:
    """The suite's ``.source.json``, exactly as written, after checking its required keys.

    Raises:
        ValueError: If the file is missing, is not a JSON object, has an unknown ``kind``, or
            lacks a key its kind requires (the message names the key).
    """
    path = source_path_for(suite_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot read the suite's source record: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: source record is not a JSON object")
    kind = data.get("kind")
    if kind not in _REQUIRED_SOURCE_KEYS:
        raise ValueError(f"{path}: 'kind' must be one of {sorted(_REQUIRED_SOURCE_KEYS)}")
    missing = [key for key in _REQUIRED_SOURCE_KEYS[kind] if key not in data]
    if missing:
        raise ValueError(f"{path}: {kind} source record lacks {', '.join(missing)}")
    return data


def source_release(source: dict[str, Any]) -> str:
    """The identity's ``source_release``: the download's release, ``n/a`` for authored."""
    return str(source["release"]) if source["kind"] == "download" else NOT_APPLICABLE


def source_adapter_version(source: dict[str, Any]) -> str:
    """The adapter version that produced the suite file, ``n/a`` for an authored suite.

    Read from ``.source.json`` rather than ``adapters.ADAPTER_VERSION``: what matters is which
    adapter *wrote the file being scored*, which a later code bump does not change until the
    suite is regenerated (and then ``suite_sha256`` moves too).
    """
    return str(source.get("adapter_version", NOT_APPLICABLE))


def package_versions() -> dict[str, str]:
    """Installed versions of ``RECORDED_PACKAGES``."""
    versions: dict[str, str] = {}
    for name in RECORDED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "not installed"
    return versions
