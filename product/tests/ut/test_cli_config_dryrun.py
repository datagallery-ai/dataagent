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
"""Tests for CLI ``--dryrun`` configuration merge output."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dataagent.interface.cli.main import run_config_dryrun
from dataagent.utils.log import logger
from dataagent.utils.runtime_paths import dataagent_package_path


def test_run_config_dryrun_prints_merged_yaml(tmp_path: Path) -> None:
    """Dryrun reloads config and emits merged YAML via logger."""
    config_path = dataagent_package_path("core", "flex", "examples", "arithmetic.yaml")
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO")
    try:
        run_config_dryrun(config_path)
    finally:
        logger.remove(sink_id)

    merged_text = "\n".join(messages)
    assert "AGENT_CONFIG:" in merged_text
    assert "arithmetic agent" in merged_text


def test_run_config_dryrun_writes_timestamped_file(tmp_path: Path) -> None:
    """Dryrun with config_output writes dataagent_config_<timestamp>.yaml under the directory."""
    config_path = dataagent_package_path("core", "flex", "examples", "arithmetic.yaml")
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="INFO")
    try:
        run_config_dryrun(config_path, config_output=tmp_path)
    finally:
        logger.remove(sink_id)

    written = list(tmp_path.glob("dataagent_config_*.yaml"))
    assert len(written) == 1
    content = written[0].read_text(encoding="utf-8")
    assert "AGENT_CONFIG:" in content
    assert any("Wrote merged configuration to" in message for message in messages)


def test_cli_dryrun_exits_without_starting_terminal_mode(tmp_path: Path) -> None:
    """``python -m dataagent --config ... --dryrun`` completes successfully."""
    config_path = dataagent_package_path("core", "flex", "examples", "arithmetic.yaml")
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "dataagent",
            "--config",
            str(config_path),
            "--dryrun",
            "--config_output",
            str(tmp_path),
        ],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(repo_root)},
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.glob("dataagent_config_*.yaml"))
