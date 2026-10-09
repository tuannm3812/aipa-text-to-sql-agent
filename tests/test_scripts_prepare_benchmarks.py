"""scripts/prepare_benchmarks.py, end to end, without the network or the real benchmarks.

Each test builds a fake archive in the benchmark's real layout from tests/fixtures, points
the script's repository root at tmp_path, and re-pins the expected SHA-256 to the fake. An
autouse fixture makes any attempt to open a URL fail the test, so nothing here can reach
the network even by mistake.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

import scripts.prepare_benchmarks as script
from text_to_sql_agent.evaluation_v2 import load_suite

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUIRED_SOURCE_KEYS = [
    "kind",
    "release",
    "url",
    "sha256",
    "licence",
    "adapter_version",
    "unpacked_at",
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin(monkeypatch: pytest.MonkeyPatch, name: str, path: Path) -> None:
    original: script.Download = getattr(script, name)
    monkeypatch.setattr(script, name, script.Download(original.url, sha(path), original.filename))


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(script, "ROOT_DIR", root)

    def no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prepare_benchmarks tried to open a URL under pytest")

    monkeypatch.setattr(script.urllib.request, "urlopen", no_network)
    return root


def spider_zip(tmp_path: Path) -> Path:
    path = tmp_path / "spider_data.zip"
    mini = FIXTURES / "spider_mini"
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(mini / "dev.json", "spider_data/dev.json")
        zf.writestr("__MACOSX/spider_data/._dev.json", b"junk")
        for db in sorted((mini / "database").iterdir()):
            zf.write(db / f"{db.name}.sqlite", f"spider_data/database/{db.name}/{db.name}.sqlite")
    return path


def bird_zip(tmp_path: Path) -> Path:
    inner = io.BytesIO()
    mini = FIXTURES / "bird_mini"
    with zipfile.ZipFile(inner, "w") as zf:
        for db in sorted((mini / "dev_databases").iterdir()):
            zf.write(db / f"{db.name}.sqlite", f"dev_databases/{db.name}/{db.name}.sqlite")
    path = tmp_path / "dev.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("dev_20240627/dev_databases.zip", inner.getvalue())
        zf.writestr("dev_20240627/dev.json", b"[]")  # the old split; must be ignored
    return path


def run(*argv: str) -> int:
    return script.main(list(argv))


# --- Hash pinning ---------------------------------------------------------------------------


def test_archive_hash_mismatch_is_a_hard_failure_printing_both_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = spider_zip(tmp_path)
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 1
    err = capsys.readouterr().err
    assert f"expected {script.SPIDER_ARCHIVE.sha256}" in err
    assert f"actual   {sha(archive)}" in err
    assert archive.exists()  # a user's file is never deleted
    assert not (script.ROOT_DIR / "evaluation" / "suites" / "spider_dev.jsonl").exists()


def test_cached_file_with_the_wrong_hash_is_refused_and_kept(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cached = repo / "data" / "benchmarks" / "_downloads" / "spider_data.zip"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"<html>Google Drive can't scan this file for viruses</html>")
    assert run("--suite", "spider_dev") == 1
    err = capsys.readouterr().err
    assert script.SPIDER_ARCHIVE.sha256 in err and sha(cached) in err
    assert "delete the cached file" in err
    assert cached.exists()


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def test_a_download_with_the_wrong_hash_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "downloads"
    monkeypatch.setattr(script.urllib.request, "urlopen", lambda *a, **k: _Response(b"nope"))
    download = script.Download("https://example.invalid/x.zip", "0" * 64, "x.zip")
    with pytest.raises(script.PrepareError) as raised:
        script.fetch(download, downloads)
    assert f"expected {'0' * 64}" in str(raised.value)
    assert f"actual   {hashlib.sha256(b'nope').hexdigest()}" in str(raised.value)
    assert list(downloads.iterdir()) == []


def test_a_download_with_the_right_hash_is_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "downloads"
    monkeypatch.setattr(script.urllib.request, "urlopen", lambda *a, **k: _Response(b"good"))
    download = script.Download(
        "https://example.invalid/x.zip", hashlib.sha256(b"good").hexdigest(), "x.zip"
    )
    assert script.fetch(download, downloads) == downloads / "x.zip"
    assert (downloads / "x.zip").read_bytes() == b"good"
    assert sorted(p.name for p in downloads.iterdir()) == ["x.zip"]


# --- Spider end to end ----------------------------------------------------------------------


def test_spider_end_to_end(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    argv = ["--suite", "spider_dev", "--archive", str(archive), "--draw-subset", "2"]
    assert run(*argv) == 0

    suites = repo / "evaluation" / "suites"
    cases = load_suite(suites / "spider_dev.jsonl")
    assert [c.id for c in cases] == ["0", "1", "2"]
    assert [c.hardness for c in cases] == ["easy", "medium", "hard"]  # from the classifier
    for case in cases:
        assert case.db_path.startswith("data/benchmarks/spider/database/")
        assert (repo / case.db_path).is_file()
    assert not (repo / "data" / "benchmarks" / "spider" / "database" / "__MACOSX").exists()

    source = json.loads((suites / "spider_dev.source.json").read_text())
    assert list(source)[: len(REQUIRED_SOURCE_KEYS)] == REQUIRED_SOURCE_KEYS
    assert source["kind"] == "download"
    assert source["sha256"] == sha(archive)
    assert source["licence"] == "CC BY-SA 4.0"
    assert source["adapter_version"] == script.ADAPTER_VERSION

    subset = suites / "spider_dev.subset2.txt"
    first = subset.read_text()
    assert len(first.splitlines()) == 2

    # Re-running reproduces the committed list rather than re-drawing it ...
    assert run(*argv) == 0
    assert subset.read_text() == first
    # ... and a list the draw does not reproduce is refused and left untouched.
    subset.write_text("not-a-draw\n")
    assert run(*argv) == 1
    assert subset.read_text() == "not-a-draw\n"


def test_dest_outside_the_repository_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    outside = tmp_path / "elsewhere"
    assert run("--suite", "spider_dev", "--archive", str(archive), "--dest", str(outside)) == 1
    assert "must be inside the repository" in capsys.readouterr().err


def test_archive_needs_exactly_one_suite(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run("--suite", "spider_dev", "bird_dev", "--archive", str(tmp_path / "x.zip"))


# --- BIRD end to end ------------------------------------------------------------------------


def place_bird_side_files(repo: Path, monkeypatch: pytest.MonkeyPatch, card: str) -> None:
    downloads = repo / "data" / "benchmarks" / "_downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    questions = downloads / script.BIRD_QUESTIONS.filename
    questions.write_bytes((FIXTURES / "bird_mini" / "dev.json").read_bytes())
    readme = downloads / script.BIRD_CARD.filename
    readme.write_text(card)
    pin(monkeypatch, "BIRD_QUESTIONS", questions)
    pin(monkeypatch, "BIRD_CARD", readme)


def test_bird_end_to_end(tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = bird_zip(tmp_path)
    pin(monkeypatch, "BIRD_ARCHIVE", archive)
    place_bird_side_files(repo, monkeypatch, "---\nlicense: cc-by-sa-4.0\n---\n# BIRD\n")
    assert run("--suite", "bird_dev", "--archive", str(archive)) == 0

    suites = repo / "evaluation" / "suites"
    cases = load_suite(suites / "bird_dev.jsonl")
    assert [c.id for c in cases] == ["100", "205", "317"]
    assert [c.hardness for c in cases] == ["easy", "medium", "hard"]
    assert cases[1].evidence == "taller than 2 metres refers to height_cm > 200"
    for case in cases:
        assert case.db_path.startswith("data/benchmarks/bird/dev_databases/")
        assert (repo / case.db_path).is_file()
    # The nested databases zip is a temporary, not a second cached copy.
    downloads = repo / "data" / "benchmarks" / "_downloads"
    assert sorted(p.name for p in downloads.iterdir()) == sorted(
        [script.BIRD_CARD.filename, script.BIRD_QUESTIONS.filename]
    )

    source = json.loads((suites / "bird_dev.source.json").read_text())
    assert list(source)[: len(REQUIRED_SOURCE_KEYS)] == REQUIRED_SOURCE_KEYS
    assert source["licence"] == "cc-by-sa-4.0"  # read from the card, not assumed
    assert source["sha256"] == sha(archive)
    assert source["questions_sha256"] == script.BIRD_QUESTIONS.sha256
    assert not (suites / "bird_dev.subset200.txt").exists()  # only with --draw-subset


def test_bird_card_without_a_licence_is_refused(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = bird_zip(tmp_path)
    pin(monkeypatch, "BIRD_ARCHIVE", archive)
    place_bird_side_files(repo, monkeypatch, "---\ntask_categories: [qa]\n---\n")
    assert run("--suite", "bird_dev", "--archive", str(archive)) == 1
    assert "declares no license" in capsys.readouterr().err


def test_bird_source_records_the_dataset_card_hash(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = bird_zip(tmp_path)
    pin(monkeypatch, "BIRD_ARCHIVE", archive)
    place_bird_side_files(repo, monkeypatch, "---\nlicense: cc-by-sa-4.0\n---\n")
    assert run("--suite", "bird_dev", "--archive", str(archive)) == 0
    source = json.loads((repo / "evaluation" / "suites" / "bird_dev.source.json").read_text())
    assert source["licence_source_sha256"] == script.BIRD_CARD.sha256
    cases = load_suite(repo / "evaluation" / "suites" / "bird_dev.jsonl")
    assert [c.expected_tables for c in cases] == [
        ("member",),
        ("superhero",),
        ("expense", "member"),
    ]


# --- Re-runs, atomic writes, corrupt archives, the safety count -----------------------------


def test_a_byte_identical_rerun_leaves_source_json_untouched(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    source = repo / "evaluation" / "suites" / "spider_dev.source.json"
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 0
    first = source.read_bytes()
    monkeypatch.setattr(script, "now_utc", lambda: "2099-01-01T00:00:00+00:00")
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 0
    assert source.read_bytes() == first  # the old unpacked_at is kept


def test_a_changed_provenance_field_rewrites_source_json(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    source = repo / "evaluation" / "suites" / "spider_dev.source.json"
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 0
    monkeypatch.setattr(script, "now_utc", lambda: "2099-01-01T00:00:00+00:00")
    monkeypatch.setattr(script, "SPIDER_RELEASE", "spider-1.0 dev (re-released)")
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 0
    written = json.loads(source.read_text())
    assert written["release"] == "spider-1.0 dev (re-released)"
    assert written["unpacked_at"] == "2099-01-01T00:00:00+00:00"


def test_a_failed_load_back_leaves_the_previous_suite_and_no_temp_file(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    suites = repo / "evaluation" / "suites"
    suites.mkdir(parents=True)
    (suites / "spider_dev.jsonl").write_text("previous\n")

    def broken_load(path: Path) -> list[Any]:
        assert path.name == "spider_dev.jsonl.tmp"  # checked before it replaces anything
        return []

    monkeypatch.setattr(script, "load_suite", broken_load)
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 1
    assert (suites / "spider_dev.jsonl").read_text() == "previous\n"
    assert not (suites / "spider_dev.jsonl.tmp").exists()
    assert not (suites / "spider_dev.source.json").exists()


def test_a_corrupt_archive_is_an_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "spider_data.zip"
    archive.write_bytes(b"this is not a zip archive")
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)  # the hash matches; the contents do not unzip
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 1
    assert "is not a readable zip archive" in capsys.readouterr().err


def test_the_refused_gold_count_passes_each_case_its_own_engine(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = spider_zip(tmp_path)
    pin(monkeypatch, "SPIDER_ARCHIVE", archive)
    seen: list[tuple[str, object]] = []

    def recording_is_safe_query(sql: str, *, engine: object = None) -> bool:
        seen.append((sql, engine))
        return True

    monkeypatch.setattr(script, "is_safe_query", recording_is_safe_query)
    assert run("--suite", "spider_dev", "--archive", str(archive)) == 0
    assert len(seen) == 3
    assert all(engine is not None for _, engine in seen)
    # One engine per database: records 1 and 2 share shop_mini.
    assert seen[1][1] is seen[2][1] and seen[0][1] is not seen[1][1]
