from __future__ import annotations

import unittest
from unittest.mock import patch

from text_to_sql_agent.gemini_manager import GeminiManager, load_google_api_keys


class FakeGoogleError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class TestGeminiManager(unittest.TestCase):
    def test_gemini_manager_loads_multiple_key_env_vars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_API_KEYS": "key-a, key-b",
                "GEMINI_API_KEY": "key-c",
            },
            clear=True,
        ):
            self.assertEqual(load_google_api_keys(), ["key-a", "key-b", "key-c"])

    def test_gemini_manager_loads_indexed_key_env_vars(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_API_KEY": "key-a",
                "GOOGLE_API_KEY_2": "key-c",
                "GOOGLE_API_KEY_1": "key-b",
                "GEMINI_API_KEY_3": "key-d",
            },
            clear=True,
        ):
            self.assertEqual(load_google_api_keys(), ["key-a", "key-b", "key-c", "key-d"])

    def test_gemini_manager_moves_to_next_key_on_492(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if api_key == "key-a":
                raise FakeGoogleError(492)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-b"])

    def test_gemini_manager_resets_key_once_on_503(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if len(calls) == 1:
                raise FakeGoogleError(503)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-a"])

    def test_gemini_manager_advances_after_repeated_503_for_same_key(self) -> None:
        manager = GeminiManager(["key-a", "key-b"])
        calls: list[str] = []

        def fake_call(api_key: str) -> str:
            calls.append(api_key)
            if api_key == "key-a":
                raise FakeGoogleError(503)
            return "SELECT 1"

        self.assertEqual(manager.run(fake_call), "SELECT 1")
        self.assertEqual(calls, ["key-a", "key-a", "key-b"])
