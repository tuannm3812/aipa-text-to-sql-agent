"""Guards the generated requirements.txt against drifting from pyproject.toml."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirement_names(lines: list[str]) -> set[str]:
    """Extract bare distribution names, dropping versions, markers, and extras."""
    names = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        name = re.split(r"[<>=!~;\[ ]", stripped, maxsplit=1)[0]
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


def test_requirements_txt_is_marked_as_generated() -> None:
    header = (ROOT / "requirements.txt").read_text().splitlines()[0]
    assert "GENERATED" in header.upper(), (
        "requirements.txt must carry the generated-file header so it is not hand-edited"
    )


def test_requirements_txt_covers_every_runtime_dependency() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    declared = _requirement_names(pyproject["project"]["dependencies"])
    exported = _requirement_names((ROOT / "requirements.txt").read_text().splitlines())

    missing = declared - exported
    assert not missing, (
        f"requirements.txt is stale, missing {sorted(missing)}. Regenerate with: "
        "uv export --no-hashes --no-dev --no-emit-project -o requirements.txt"
    )
