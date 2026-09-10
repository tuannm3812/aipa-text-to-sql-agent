"""Environment variable loading, tolerant of a missing `python-dotenv`."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

load_dotenv: Callable[..., bool] | None
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    load_dotenv = None


def load_env(env_path: str | None = None) -> None:
    """Load environment variables from a `.env` file, if `python-dotenv` is installed.

    Args:
        env_path: Path to a specific `.env` file. When omitted, loads
            `.env` from the repository root (the parent of this package).

    Returns:
        None. Silently does nothing if `python-dotenv` is not installed.
    """
    if load_dotenv is None:
        return

    if env_path is not None:
        load_dotenv(env_path)
        return

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
