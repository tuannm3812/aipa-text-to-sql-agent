"""Run identity: what names a v2 result directory, and what does not (spec §4.4).

The identity payload holds only the immutable fields that define what is measured. Start
time, duration, outage count and status live outside it, so finishing or resuming a run never
changes its directory name. The directory is allocated with an exclusive ``mkdir`` and a
random nonce, so two identical runs never share - or overwrite - a directory.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from text_to_sql_agent.evaluation_v2 import identity as identity_module
from text_to_sql_agent.evaluation_v2.identity import (
    IdentityPayload,
    allocate_run_dir,
    config_label,
    database_fingerprint,
    git_state,
    identity_diff,
    identity_hash,
    retry_policy,
    sanitise,
)
from text_to_sql_agent.evaluation_v2.manifest import Manifest, manifest_sha256

STARTED = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)

SPEC_FIELDS = (
    "commit",
    "dirty",
    "suite",
    "suite_sha256",
    "subset",
    "subset_sha256",
    "source_release",
    "database_fingerprint",
    "adapter_version",
    "scorer_version",
    "prompt_sha256",
    "provider",
    "model",
    "evidence",
    "use_rag",
    "rag_top_k",
    "work_limit",
    "max_rows",
    "max_repair_attempts",
    "retry_policy",
)


def _payload(**overrides: object) -> IdentityPayload:
    fields: dict[str, object] = {
        "commit": "a" * 40,
        "dirty": False,
        "suite": "demo",
        "suite_sha256": "b" * 64,
        "subset": "full",
        "subset_sha256": "",
        "source_release": "n/a",
        "database_fingerprint": (("data/demo.db", "d" * 64),),
        "adapter_version": "n/a",
        "scorer_version": "2",
        "prompt_sha256": "c" * 64,
        "provider": "ollama",
        "model": "llama3:latest",
        "evidence": False,
        "use_rag": True,
        "rag_top_k": 6,
        "work_limit": 100_000,
        "max_rows": 1_000,
        "max_repair_attempts": 1,
        "retry_policy": "0x20.0",
    }
    fields.update(overrides)
    return IdentityPayload(**fields)  # type: ignore[arg-type]


def _manifest(payload: IdentityPayload, **overrides: object) -> Manifest:
    fields: dict[str, object] = {
        "identity": payload,
        "run_id": "run",
        "mode": "llm",
        "case_count": 12,
        "source": {"kind": "authored", "author": "repository", "licence": "MIT"},
        "started": STARTED.isoformat(),
        "duration_s": 1.5,
        "outage_count": 0,
        "status": "complete",
        "generation_failures": 0,
        "python": "3.12.0",
        "packages": {"sqlglot": "27.0.0"},
    }
    fields.update(overrides)
    return Manifest(**fields)  # type: ignore[arg-type]


def test_payload_fields_are_exactly_the_spec_fields_in_order() -> None:
    assert tuple(f.name for f in dataclasses.fields(IdentityPayload)) == SPEC_FIELDS


def test_identity_hash_is_sha256_hex_and_deterministic() -> None:
    digest = identity_hash(_payload())
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    assert digest == identity_hash(_payload())


@pytest.mark.parametrize(
    "field,value",
    [
        ("rag_top_k", 3),
        ("evidence", True),
        ("use_rag", False),
        ("dirty", True),
        ("prompt_sha256", "d" * 64),
        ("retry_policy", "3x20.0"),
        ("database_fingerprint", (("data/demo.db", "e" * 64),)),
    ],
)
def test_identity_hash_changes_with_every_immutable_field(field: str, value: object) -> None:
    assert identity_hash(_payload(**{field: value})) != identity_hash(_payload())


def test_identity_hash_ignores_status_duration_outages_and_start_time() -> None:
    payload = _payload()
    finished = _manifest(payload).to_dict()
    resumed = _manifest(
        payload,
        started="2026-10-10T09:30:00+00:00",
        duration_s=987.0,
        outage_count=4,
        status="incomplete",
    ).to_dict()
    assert finished["identity_sha256"] == resumed["identity_sha256"] == identity_hash(payload)


def test_two_allocations_with_a_frozen_start_differ_only_in_the_nonce(tmp_path: Path) -> None:
    payload = _payload()
    label = config_label(use_rag=True, rag_top_k=6, evidence=False)
    first = allocate_run_dir(tmp_path, payload, started=STARTED, config=label)
    second = allocate_run_dir(tmp_path, payload, started=STARTED, config=label)

    assert first != second
    assert first.is_dir() and second.is_dir()
    assert first.name[:-4] == second.name[:-4]
    assert first.name[-4:] != second.name[-4:]
    expected_prefix = (
        f"2026-10-09T120000_demo_full_ollama_llama3-latest_rag-on-k6-evidence-off_"
        f"{identity_hash(payload)[:8]}_"
    )
    assert first.name.startswith(expected_prefix)
    assert all(ch in "0123456789abcdef" for ch in first.name[-4:])


def test_allocation_redraws_the_nonce_on_collision_and_never_reuses_a_directory(
    tmp_path: Path,
) -> None:
    payload = _payload()
    with patch.object(identity_module.os, "urandom", side_effect=[b"\x00\x01"]):
        first = allocate_run_dir(tmp_path, payload, started=STARTED, config="gold")
    (first / "manifest.json").write_text('{"status": "complete"}', encoding="utf-8")

    # The same nonce comes up again: the exclusive mkdir must refuse it and draw again.
    with patch.object(identity_module.os, "urandom", side_effect=[b"\x00\x01", b"\x00\x02"]):
        second = allocate_run_dir(tmp_path, payload, started=STARTED, config="gold")

    assert first.name.endswith("_0001")
    assert second.name.endswith("_0002")
    assert (first / "manifest.json").read_text(encoding="utf-8") == '{"status": "complete"}'
    assert list(second.iterdir()) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ollama/llama3:latest", "ollama-llama3-latest"),
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        ("qwen2.5-coder:7b-instruct_q4", "qwen2.5-coder-7b-instruct_q4"),
        ("a b\\c", "a-b-c"),
    ],
)
def test_sanitise_keeps_a_filesystem_safe_alphabet(raw: str, expected: str) -> None:
    assert sanitise(raw) == expected


def test_sanitised_model_name_yields_a_valid_single_path_component(tmp_path: Path) -> None:
    run_dir = allocate_run_dir(
        tmp_path, _payload(model="org/model:tag"), started=STARTED, config="gold"
    )
    assert run_dir.parent == tmp_path
    assert "_org-model-tag_" in run_dir.name


def test_config_label_encodes_rag_and_evidence() -> None:
    assert config_label(use_rag=True, rag_top_k=5, evidence=True) == "rag-on-k5-evidence-on"
    assert config_label(use_rag=False, rag_top_k=5, evidence=False) == "rag-off-evidence-off"


def test_retry_policy_is_stable_across_int_and_float_seconds() -> None:
    assert retry_policy(3, 20) == retry_policy(3, 20.0) == "3x20.0"


def test_identity_diff_names_every_differing_field() -> None:
    saved = dataclasses.asdict(_payload())
    assert identity_diff(saved, _payload()) == []
    assert identity_diff(saved, _payload(rag_top_k=3, evidence=True)) == ["evidence", "rag_top_k"]


def test_identity_diff_names_a_field_missing_from_the_saved_manifest() -> None:
    saved = dataclasses.asdict(_payload())
    del saved["retry_policy"]
    assert identity_diff(saved, _payload()) == ["retry_policy"]


@pytest.mark.parametrize(
    "dirty,status,citable",
    [
        (True, "complete", False),
        (True, "incomplete", False),
        (False, "incomplete", False),
        (False, "complete", True),
    ],
)
def test_only_a_clean_complete_run_is_citable(dirty: bool, status: str, citable: bool) -> None:
    manifest = _manifest(_payload(dirty=dirty), status=status)
    assert manifest.citable is citable
    assert manifest.to_dict()["citable"] is citable


def test_manifest_sha256_is_computed_with_its_own_field_blank() -> None:
    data = _manifest(_payload()).to_dict()
    assert data["manifest_sha256"] == manifest_sha256({**data, "manifest_sha256": ""})
    assert data["manifest_sha256"] == manifest_sha256(data)  # the field's value is ignored
    assert manifest_sha256({**data, "duration_s": 2.0}) != data["manifest_sha256"]


def test_manifest_round_trips_through_its_dict() -> None:
    manifest = _manifest(_payload(), source={"kind": "download", "x": [1, {"y": 2}]})
    assert Manifest.from_dict(manifest.to_dict()) == manifest


def test_the_fingerprint_survives_a_json_round_trip() -> None:
    # JSON turns the fingerprint's tuples into lists; a resume or a regression gate reading the
    # manifest back must still see the same identity.
    payload = _payload(database_fingerprint=(("data/a.db", "1" * 64), ("data/b.db", "2" * 64)))
    saved = json.loads(json.dumps(_manifest(payload).to_dict()))
    assert identity_diff(saved, payload) == []
    assert Manifest.from_dict(saved).identity == payload


# --- database fingerprint ------------------------------------------------------------------


def _db(root: Path, relative: str, content: bytes) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return str(path)


def test_the_fingerprint_is_each_files_sha256_keyed_by_repo_relative_path(
    tmp_path: Path,
) -> None:
    import hashlib

    a = _db(tmp_path, "data/a.db", b"alpha")
    b = _db(tmp_path, "data/sub/b.db", b"beta")
    # Relative and absolute spellings of one file are one database; output is sorted.
    fingerprint = database_fingerprint([b, a, "data/a.db"], root=tmp_path)
    assert fingerprint == (
        ("data/a.db", hashlib.sha256(b"alpha").hexdigest()),
        ("data/sub/b.db", hashlib.sha256(b"beta").hexdigest()),
    )


def test_the_fingerprint_changes_when_one_database_byte_changes(tmp_path: Path) -> None:
    path = _db(tmp_path, "data/a.db", b"\x00" * 5000)
    before = database_fingerprint([path], root=tmp_path)
    with open(path, "r+b") as handle:
        handle.seek(4321)
        handle.write(b"\x01")
    after = database_fingerprint([path], root=tmp_path)
    assert before != after
    assert identity_hash(_payload(database_fingerprint=before)) != identity_hash(
        _payload(database_fingerprint=after)
    )


def test_identical_copies_in_two_checkouts_have_the_same_fingerprint(tmp_path: Path) -> None:
    one = _db(tmp_path / "checkout-1", "data/a.db", b"same bytes")
    two = _db(tmp_path / "checkout-2", "data/a.db", b"same bytes")
    assert database_fingerprint([one], root=tmp_path / "checkout-1") == database_fingerprint(
        [two], root=tmp_path / "checkout-2"
    )


def test_a_database_outside_the_root_is_keyed_by_its_absolute_path(tmp_path: Path) -> None:
    outside = _db(tmp_path / "elsewhere", "x.db", b"x")
    ((key, _),) = database_fingerprint([outside], root=tmp_path / "repo")
    assert key == Path(outside).as_posix()


@pytest.mark.parametrize("dsn", ["postgresql://u:secret@h/db", "postgres://u:secret@h/db"])
def test_a_server_dsn_cannot_be_fingerprinted_and_is_refused(tmp_path: Path, dsn: str) -> None:
    with pytest.raises(ValueError, match="server-side fingerprint is not implemented") as caught:
        database_fingerprint([dsn], root=tmp_path)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("scheme", ["sqlite", "duckdb"])
def test_a_file_dsn_fingerprints_the_file_it_names(tmp_path: Path, scheme: str) -> None:
    path = _db(tmp_path, "data/a.db", b"file bytes")
    assert database_fingerprint([f"{scheme}://{path}"], root=tmp_path) == database_fingerprint(
        [path], root=tmp_path
    )


def test_fingerprint_changes_name_only_the_databases_that_differ() -> None:
    from text_to_sql_agent.evaluation_v2.identity import fingerprint_changes

    old = [["data/a.db", "1" * 64], ["data/b.db", "2" * 64], ["data/c.db", "3" * 64]]
    new = (("data/a.db", "1" * 64), ("data/b.db", "9" * 64), ("data/d.db", "4" * 64))
    changes = fingerprint_changes(old, new)
    assert changes == [
        f"data/b.db ({'2' * 12} -> {'9' * 12})",
        "data/c.db (no longer used)",
        "data/d.db (newly used)",
    ]
    assert fingerprint_changes(old, [list(pair) for pair in old]) == []


def test_git_state_reports_this_checkout_head() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    commit, dirty = git_state()
    assert commit == head
    assert isinstance(dirty, bool)


def test_git_state_ignores_superpowers_scratch_and_untracked_files() -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, stdout="f" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout=" M .superpowers/sdd/progress.md\n", stderr=""
        )

    with patch.object(identity_module.subprocess, "run", side_effect=fake_run):
        assert git_state() == ("f" * 40, False)


def test_git_state_reports_a_modified_tracked_file_as_dirty() -> None:
    def fake_run(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, stdout="f" * 40 + "\n", stderr="")
        return subprocess.CompletedProcess(
            args, 0, stdout=" M text_to_sql_agent/llm.py\n", stderr=""
        )

    with patch.object(identity_module.subprocess, "run", side_effect=fake_run):
        assert git_state() == ("f" * 40, True)
