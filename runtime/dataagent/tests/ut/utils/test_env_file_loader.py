# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for ``dataagent.utils.env_file_loader``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dataagent.utils.env_file_loader import load_env_file


def _snapshot(keys: set[str]) -> dict[str, str]:
    return {key: os.environ[key] for key in keys if key in os.environ}


def _restore_env(original: dict[str, str | None], keys: set[str]) -> None:
    for key in keys:
        if original.get(key) is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original[key]  # type: ignore[assignment]


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Track and restore selected environment variables after each test."""
    keys = {
        "DATAAGENT_LOG_LEVEL",
        "TAVILY_API_KEY",
        "DEPLOY_ZONE",
        "A",
        "B",
        "C",
        "FOO",
        "QUOT",
        "ESC",
        "EMPTY_KEY",
        "X",
    }
    original = {key: os.environ.get(key) for key in keys}
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    yield keys
    _restore_env(original, keys)


def test_load_env_file_project_style(tmp_path: Path, isolated_env: set[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'DATAAGENT_LOG_LEVEL="INFO"',
                'TAVILY_API_KEY="" # comment',
                'DEPLOY_ZONE="" # 中文注释',
            ]
        ),
        encoding="utf-8",
    )

    assert load_env_file(env_file) is True
    assert os.environ["DATAAGENT_LOG_LEVEL"] == "INFO"
    assert os.environ["TAVILY_API_KEY"] == ""
    assert os.environ["DEPLOY_ZONE"] == ""


def test_load_env_file_does_not_override_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAAGENT_LOG_LEVEL", "DEBUG")
    env_file = tmp_path / ".env"
    env_file.write_text('DATAAGENT_LOG_LEVEL="INFO"\n', encoding="utf-8")

    assert load_env_file(env_file) is True
    assert os.environ["DATAAGENT_LOG_LEVEL"] == "DEBUG"


def test_load_env_file_interpolation(tmp_path: Path, isolated_env: set[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=hello\nB=${A}\nC=${X:-default}\n", encoding="utf-8")

    assert load_env_file(env_file) is True
    assert os.environ["A"] == "hello"
    assert os.environ["B"] == "hello"
    assert os.environ["C"] == "default"


def test_load_env_file_export_quotes_and_escapes(tmp_path: Path, isolated_env: set[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        'export FOO="bar"\nQUOT=\'a\\\'b\'\nESC="line1\\nline2"\n',
        encoding="utf-8",
    )

    assert load_env_file(env_file) is True
    assert os.environ["FOO"] == "bar"
    assert os.environ["QUOT"] == "a'b"
    assert os.environ["ESC"] == "line1\nline2"


def test_load_env_file_bare_key_not_exported(tmp_path: Path, isolated_env: set[str]) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("EMPTY_KEY\n", encoding="utf-8")

    assert load_env_file(env_file) is True
    assert "EMPTY_KEY" not in os.environ


def test_load_env_file_empty_or_comment_only(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.env"
    empty_file.write_text("", encoding="utf-8")
    assert load_env_file(empty_file) is False

    comment_file = tmp_path / "comments.env"
    comment_file.write_text("# only comment\n\n# another\n", encoding="utf-8")
    assert load_env_file(comment_file) is False


def test_load_env_file_returns_true_when_all_keys_preexist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATAAGENT_LOG_LEVEL", "DEBUG")
    env_file = tmp_path / ".env"
    env_file.write_text('DATAAGENT_LOG_LEVEL="INFO"\n', encoding="utf-8")

    assert load_env_file(env_file) is True


def test_load_env_file_matches_python_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional parity check against transitive python-dotenv when available."""
    dotenv = pytest.importorskip("dotenv")
    from dotenv import load_dotenv

    cases = [
        'DATAAGENT_LOG_LEVEL="INFO"\nTAVILY_API_KEY="" # comment\n',
        "A=hello\nB=${A}\nC=${X:-default}\n",
        'export FOO="bar"\nQUOT=\'a\\\'b\'\nESC="line1\\nline2"\n',
        "EMPTY_KEY\nKEY=value\n",
    ]
    keys = ["DATAAGENT_LOG_LEVEL", "TAVILY_API_KEY", "A", "B", "C", "FOO", "QUOT", "ESC", "KEY"]

    for content in cases:
        for key in keys:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRESET", "from-os")

        env_file = tmp_path / "parity.env"
        env_file.write_text(content, encoding="utf-8")

        expected_path = tmp_path / "expected.env"
        expected_path.write_text(content, encoding="utf-8")
        assert load_dotenv(expected_path) is True
        expected = _snapshot(set(keys) | {"PRESET"})

        for key in keys:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("PRESET", "from-os")

        actual_path = tmp_path / "actual.env"
        actual_path.write_text(content, encoding="utf-8")
        assert load_env_file(actual_path) is True
        actual = _snapshot(set(keys) | {"PRESET"})

        assert actual == expected, content

        monkeypatch.delenv("PRESET", raising=False)
        for key in keys:
            monkeypatch.delenv(key, raising=False)

        _ = dotenv
