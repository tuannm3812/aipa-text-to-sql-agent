"""Run identity: what names a v2 result directory, and what does not (spec §4.4).

The identity payload holds only the immutable fields that define what is measured. Start
time, duration, outage count and status live outside it, so finishing or resuming a run never
changes its directory name. The directory is allocated with an exclusive ``mkdir`` and a
random nonce, so two identical runs never share - or overwrite - a directory.
"""

from __future__ import annotations

import dataclasses
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
