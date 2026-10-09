"""Download, verify, unpack and convert Spider 1.0 dev and BIRD dev into v2 suites.

    uv run python scripts/prepare_benchmarks.py --suite spider_dev bird_dev
    uv run python scripts/prepare_benchmarks.py --suite spider_dev bird_dev \\
        --draw-subset 200 --seed 20261008

For each suite this writes, under the repository root:

  data/benchmarks/<benchmark>/...              the dev databases (gitignored)
  evaluation/suites/<suite>.jsonl              the converted suite (gitignored)
  evaluation/suites/<suite>.source.json        release, URL, SHA-256, licence (tracked)
  evaluation/suites/<suite>.subset<N>.txt      with --draw-subset N (tracked)

Every download is pinned by SHA-256 in this file. A mismatch - a moved file, a Google Drive
interstitial page instead of the archive, a truncated cache - is a hard failure that prints
both hashes; nothing is converted from an unverified file, and a cached file is never
silently re-downloaded over.

Archives are cached in <dest>/_downloads/ and reused when their hash still matches. If an
automatic download stops working (Spider's Google Drive link is the likeliest to), download
the archive by hand from the URL printed in the error and pass it with --archive PATH; it is
verified against the same pinned hash and is never moved or deleted.

A committed subset list is never re-drawn: if <suite>.subset<N>.txt exists and the draw
does not reproduce it byte for byte, the script refuses rather than overwrite it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Imports below must follow the sys.path insert above, hence E402.
from text_to_sql_agent import is_safe_query  # noqa: E402
from text_to_sql_agent.engines import Engine, open_engine  # noqa: E402
from text_to_sql_agent.evaluation_v2 import Case, SuiteError, load_suite  # noqa: E402
from text_to_sql_agent.evaluation_v2.adapters import (  # noqa: E402
    ADAPTER_VERSION,
    BIRD_SUITE,
    SPIDER_SUITE,
    bird_to_cases,
    database_path,
    draw_subset,
    spider_to_cases,
)
from text_to_sql_agent.evaluation_v2.spider_hardness import spider_hardness  # noqa: E402

SUBSET_SEED = 20261008
SQLITE_MAGIC = b"SQLite format 3\x00"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class Download:
    """One pinned file: where it comes from, what it must hash to, its cache name."""

    url: str
    sha256: str
    filename: str


# --- Spider 1.0 -----------------------------------------------------------------------------
# The official page (https://yale-lily.github.io/spider) links one Google Drive file,
# spider_data.zip (uploaded 2024-09-11; its dev.json is the 2020-08-03 revision, 1,034
# questions over 20 databases). drive.usercontent.google.com with confirm=t is that same
# file's direct-download endpoint, which skips the "can't scan for viruses" page.
SPIDER_ARCHIVE = Download(
    url=(
        "https://drive.usercontent.google.com/download"
        "?id=1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J&export=download&confirm=t"
    ),
    sha256="00636695dabed6b5f4b8328a16b13e069a2f16591d5efcce57660669c85b121b",
    filename="spider_data.zip",
)
SPIDER_RELEASE = "spider-1.0 dev (spider_data.zip, dev.json revised 2020-08-03)"
# spider_data.zip bundles no licence file (README.txt has citations only), so the licence
# is taken from the official page, and the source says so.
SPIDER_LICENCE = "CC BY-SA 4.0"
SPIDER_LICENCE_SOURCE = "https://yale-lily.github.io/spider (the archive bundles no licence file)"

# --- BIRD dev, 2025-11-13 development split -------------------------------------------------
# bird-bench.github.io's 2025-11-13 news item announces "bird-sql-dev-1106", a cleaned dev
# split published on Hugging Face as questions only (1,534; dataset card dated 2025-11-06).
# Its card says existing users keep the databases they have, so the databases come from the
# official dev.zip on BIRD's Alibaba Cloud bucket (inner folder dev_20240627). Both are
# pinned; the Hugging Face files are addressed by commit, not by branch, so they cannot move.
BIRD_HF_REVISION = "3c11fb193e5439b338e23677fa0aae11e8b85db9"
_BIRD_HF = (
    f"https://huggingface.co/datasets/birdsql/bird_sql_dev_20251106/resolve/{BIRD_HF_REVISION}"
)
BIRD_ARCHIVE = Download(
    url="https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip",
    sha256="cdd6d19faeb45a23970b98d3ef6c40a87987c95459c2cf12076897a60cf5a630",
    filename="bird_dev.zip",
)
BIRD_QUESTIONS = Download(
    url=f"{_BIRD_HF}/data/dev_20251106-00000-of-00001.json",
    sha256="ffd8018378ddb1a8794753e0a31cfc81862ff7318a5184c22f3dc4ce03a03feb",
    filename="bird_dev_20251106.json",
)
BIRD_CARD = Download(
    url=f"{_BIRD_HF}/README.md",
    sha256="2ed0fcf8e873ef953d70bcf5e96042155b86a534d7b7c550509f7903dbfaeed5",
    filename="bird_dev_20251106_README.md",
)
BIRD_RELEASE = (
    "bird-sql-dev-20251106 (2025-11-13 development split; questions "
    f"birdsql/bird_sql_dev_20251106@{BIRD_HF_REVISION[:12]}, databases dev.zip dev_20240627)"
)
BIRD_DATABASES_MEMBER = "dev_20240627/dev_databases.zip"
# dev.zip bundles no licence file; the release's own dataset card declares one in its
# front matter, and that is what is recorded. bird-bench.github.io says the same (CC BY-SA
# 4.0 from 2024-04-27), so the two sources agree.
BIRD_LICENCE_SOURCE = (
    f"front matter of birdsql/bird_sql_dev_20251106@{BIRD_HF_REVISION[:12]} README.md "
    "(dev.zip bundles no licence file; bird-bench.github.io agrees: CC BY-SA 4.0 "
    "since 2024-04-27)"
)

SUITES = ("spider_dev", "bird_dev")


class PrepareError(Exception):
    """A download, verification or conversion step failed; nothing further is written."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, download: Download) -> None:
    actual = sha256_file(path)
    if actual != download.sha256:
        raise PrepareError(
            f"SHA-256 mismatch for {path}\n"
            f"  expected {download.sha256}\n"
            f"  actual   {actual}\n"
            f"  source   {download.url}"
        )


def fetch(download: Download, downloads: Path, *, override: Path | None = None) -> Path:
    """A verified local copy of ``download``: the override, the cache, or a fresh download."""
    if override is not None:
        if not override.is_file():
            raise PrepareError(f"--archive {override} is not a file")
        verify(override, download)  # a user's file is reported, never deleted
        return override
    target = downloads / download.filename
    if target.exists():
        try:
            verify(target, download)
        except PrepareError as exc:
            raise PrepareError(f"{exc}\n  delete the cached file to download it again") from exc
        print(f"cached   {target}")
        return target
    downloads.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    print(f"download {download.url}\n      -> {target}")
    request = urllib.request.Request(
        download.url, headers={"User-Agent": "text-to-sql-agent/prepare_benchmarks"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out, _CHUNK)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise PrepareError(
            f"download failed: {download.url}: {exc}\n"
            "  download it by hand and pass it with --archive PATH"
        ) from exc
    try:
        verify(partial, download)
    except PrepareError:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


@contextmanager
def open_zip(path: Path) -> Iterator[zipfile.ZipFile]:
    """``zipfile.ZipFile(path)``, with a corrupt archive reported as a ``PrepareError``."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise PrepareError(f"{path} is not a readable zip archive: {exc}") from exc
    with archive:
        yield archive


def extract_member(archive: zipfile.ZipFile, member: str, target: Path) -> None:
    """Copy one named member out of ``archive``; names are built here, never taken from it."""
    try:
        info = archive.getinfo(member)
    except KeyError as exc:
        raise PrepareError(f"{archive.filename}: missing expected member {member}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with archive.open(info) as source, target.open("wb") as out:
            shutil.copyfileobj(source, out, _CHUNK)
    except zipfile.BadZipFile as exc:
        raise PrepareError(f"{archive.filename}: corrupt member {member}: {exc}") from exc


def database_paths(dev: list[Any], db_root: Path, suite: str) -> dict[str, str]:
    """``db_id`` -> repo-relative ``db_path`` for every database the source records name.

    Records without a usable ``db_id`` are skipped here; the adapter rejects them with a
    message naming the record once the databases they do name are available.
    """
    db_ids = {
        record["db_id"]
        for record in dev
        if isinstance(record, dict) and isinstance(record.get("db_id"), str) and record["db_id"]
    }
    return {db_id: database_path(db_root, db_id, suite=suite) for db_id in sorted(db_ids)}


def extract_databases(
    archive: zipfile.ZipFile, prefix: str, paths: dict[str, str]
) -> dict[str, list[str]]:
    """Extract ``<prefix>/<db_id>/<db_id>.sqlite`` for each database; return its table names."""
    tables: dict[str, list[str]] = {}
    for db_id, db_path in paths.items():
        target = ROOT_DIR / db_path
        extract_member(archive, f"{prefix}/{db_id}/{db_id}.sqlite", target)
        with target.open("rb") as handle:
            if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                raise PrepareError(f"{target} is not a SQLite database")
        tables[db_id] = table_names(target)
    return tables


def table_names(path: Path) -> list[str]:
    """Every table and view name in a SQLite file, read through a read-only connection."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall()
    return [str(name) for (name,) in rows]


def relative_to_repo(path: Path) -> Path:
    try:
        return path.resolve().relative_to(ROOT_DIR.resolve())
    except ValueError as exc:
        raise PrepareError(
            f"--dest must be inside the repository ({ROOT_DIR}) so db_path stays repo-relative"
        ) from exc


def read_card_licence(card: Path) -> str:
    """The ``license:`` value from a Hugging Face dataset card's YAML front matter."""
    lines = card.read_text(encoding="utf-8").split("\n")
    if not lines or lines[0].strip() != "---":
        raise PrepareError(f"{card}: no front matter")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key.strip() == "license" and value.strip():
            return value.strip()
    raise PrepareError(f"{card}: front matter declares no license")


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def source_block(download: Download, release: str, licence: str) -> dict[str, Any]:
    """The block the run manifest copies verbatim; required keys first, extras after."""
    return {
        "kind": "download",
        "release": release,
        "url": download.url,
        "sha256": download.sha256,
        "licence": licence,
        "adapter_version": ADAPTER_VERSION,
        "unpacked_at": now_utc(),
    }


def prepare_spider(dest: Path, archive: Path | None) -> tuple[list[Case], dict[str, Any]]:
    path = fetch(SPIDER_ARCHIVE, dest / "_downloads", override=archive)
    root = dest / "spider"
    with open_zip(path) as zf:
        try:
            raw = zf.read("spider_data/dev.json")
        except KeyError as exc:
            raise PrepareError(f"{path}: missing spider_data/dev.json") from exc
        dev = json.loads(raw)
        db_root = relative_to_repo(root) / "database"
        tables = extract_databases(
            zf, "spider_data/database", database_paths(dev, db_root, SPIDER_SUITE)
        )
    hardness = {str(i): spider_hardness(record["sql"]) for i, record in enumerate(dev)}
    cases = spider_to_cases(dev, db_root=db_root, hardness=hardness, tables=tables)
    root.mkdir(parents=True, exist_ok=True)
    (root / "dev.json").write_bytes(raw)
    source = source_block(SPIDER_ARCHIVE, SPIDER_RELEASE, SPIDER_LICENCE)
    source["licence_source"] = SPIDER_LICENCE_SOURCE
    return cases, source


def prepare_bird(dest: Path, archive: Path | None) -> tuple[list[Case], dict[str, Any]]:
    downloads = dest / "_downloads"
    path = fetch(BIRD_ARCHIVE, downloads, override=archive)
    questions = fetch(BIRD_QUESTIONS, downloads)
    card = fetch(BIRD_CARD, downloads)
    licence = read_card_licence(card)
    root = dest / "bird"
    dev = json.loads(questions.read_text(encoding="utf-8"))
    db_root = relative_to_repo(root) / "dev_databases"
    paths = database_paths(dev, db_root, BIRD_SUITE)
    # The databases are a zip inside the zip; spill it to a temporary file next to the
    # cache (so it shares a filesystem with the destination), extract, and remove it.
    with open_zip(path) as outer, tempfile.TemporaryDirectory(dir=downloads) as tmp:
        inner_path = Path(tmp) / "dev_databases.zip"
        extract_member(outer, BIRD_DATABASES_MEMBER, inner_path)
        with open_zip(inner_path) as inner:
            tables = extract_databases(inner, "dev_databases", paths)
    cases = bird_to_cases(dev, db_root=db_root, tables=tables)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(questions, root / BIRD_QUESTIONS.filename)
    source = source_block(BIRD_ARCHIVE, BIRD_RELEASE, licence)
    source["questions_url"] = BIRD_QUESTIONS.url
    source["questions_sha256"] = BIRD_QUESTIONS.sha256
    source["licence_source"] = BIRD_LICENCE_SOURCE
    source["licence_source_sha256"] = BIRD_CARD.sha256
    return cases, source


def write_atomically(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_suite(suite: str, cases: list[Case], source: dict[str, Any], suites: Path) -> Path:
    """Write the suite and its source block; neither is touched unless both are sound.

    The suite goes to ``<suite>.jsonl.tmp`` first and replaces ``<suite>.jsonl`` only after
    loading back to exactly ``cases``, so a failed check never leaves a half-written suite.
    """
    suites.mkdir(parents=True, exist_ok=True)
    path = suites / f"{suite}.jsonl"
    tmp = path.with_name(path.name + ".tmp")
    text = "".join(json.dumps(asdict(case), ensure_ascii=False) + "\n" for case in cases)
    try:
        tmp.write_text(text, encoding="utf-8")
        if load_suite(tmp) != cases:  # the same check every later run applies
            raise PrepareError(f"{tmp} does not load back to the cases that were written")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    write_source(suites / f"{suite}.source.json", source)
    return path


def write_source(path: Path, source: dict[str, Any]) -> None:
    """Write ``source`` unless only ``unpacked_at`` differs from what is already there.

    ``.source.json`` is tracked; rewriting it for a new timestamp alone would leave the tree
    dirty after every byte-identical rerun. The kept timestamp is the first unpack that
    produced this exact provenance.
    """
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        stamp = "unpacked_at"
        if {k: v for k, v in existing.items() if k != stamp} == {
            k: v for k, v in source.items() if k != stamp
        }:
            print(f"source   {path} unchanged")
            return
    write_atomically(path, json.dumps(source, indent=2, ensure_ascii=False) + "\n")
    print(f"source   {path} written")


def write_subset(suite: str, cases: list[Case], size: int, seed: int, suites: Path) -> Path:
    ids = draw_subset(cases, size=size, seed=seed)
    path = suites / f"{suite}.subset{size}.txt"
    text = "".join(f"{case_id}\n" for case_id in ids)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise PrepareError(
                f"{path} exists and this draw (size {size}, seed {seed}) does not reproduce "
                "it; a committed subset is never re-drawn, so it was left untouched"
            )
        print(f"subset   {path} reproduced")
    else:
        write_atomically(path, text)
        print(f"subset   {path} written")
    by_id = {case.id: case.hardness for case in cases}
    print(f"         hardness {dict(sorted(Counter(by_id[i] for i in ids).items()))}")
    return path


def refused_gold(cases: list[Case]) -> list[str]:
    """IDs whose gold SQL ``is_safe_query`` refuses, checked as ``run_gold`` checks it.

    ``run_gold`` passes the case's own engine, so this does too; nothing is executed.
    """
    engines: dict[str, Engine] = {}
    refused: list[str] = []
    for case in cases:
        if case.db_path not in engines:
            engines[case.db_path] = open_engine(str(ROOT_DIR / case.db_path))
        if not is_safe_query(case.gold_sql, engine=engines[case.db_path]):
            refused.append(case.id)
    return refused


def summarise(suite: str, cases: list[Case]) -> None:
    hardness = dict(sorted(Counter(case.hardness for case in cases).items()))
    refused = refused_gold(cases)
    covered = sum(1 for case in cases if case.expected_tables)
    widest = max(len(case.expected_tables) for case in cases)
    print(f"{suite}: {len(cases)} cases, hardness {hardness}")
    print(f"         expected_tables: {covered} cases with >= 1 table, max {widest} per case")
    print(f"         gold refused by is_safe_query: {len(refused)} {refused[:10]}")


PREPARERS = {"spider_dev": prepare_spider, "bird_dev": prepare_bird}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--suite", nargs="+", choices=SUITES, required=True)
    parser.add_argument(
        "--dest",
        type=Path,
        default=ROOT_DIR / "data" / "benchmarks",
        help="where databases and the download cache go; must be inside the repository",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help=(
            "a manually downloaded copy of the suite's main archive (Spider: spider_data.zip; "
            "BIRD: dev.zip), verified against the pinned SHA-256; needs exactly one --suite"
        ),
    )
    parser.add_argument(
        "--draw-subset", type=int, metavar="N", help="also write <suite>.subset<N>.txt"
    )
    parser.add_argument("--seed", type=int, default=SUBSET_SEED)
    args = parser.parse_args(argv)
    if args.archive is not None and len(args.suite) != 1:
        parser.error("--archive needs exactly one --suite")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dest = args.dest if args.dest.is_absolute() else Path.cwd() / args.dest
    suites = ROOT_DIR / "evaluation" / "suites"
    try:
        for suite in dict.fromkeys(args.suite):
            cases, source = PREPARERS[suite](dest, args.archive)
            print(f"suite    {write_suite(suite, cases, source, suites)}")
            if args.draw_subset is not None:
                write_subset(suite, cases, args.draw_subset, args.seed, suites)
            summarise(suite, cases)
    except (PrepareError, SuiteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
