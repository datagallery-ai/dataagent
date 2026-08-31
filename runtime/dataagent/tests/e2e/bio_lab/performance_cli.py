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

"""Tool and single-query modes for the bio_lab performance e2e test."""

import shutil
import tempfile
from pathlib import Path

from loguru import logger
from performance_config import (
    _DEFAULT_AGENT_CONFIG_FILE,
    _ORIGINAL_SQLITE_PATH,
    _build_cache_test_config,
    _load_agent_config,
    _resolve_config_paths,
)
from performance_semantic_mock import _apply_semantic_layer_config


# ---------------------------------------------------------------------------
# Tool mode — interactive CLI with mock services
# ---------------------------------------------------------------------------
def _create_test_workspace() -> Path:
    """Create a temporary workspace directory and copy the sqlite DB into it."""
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_bio_lab_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "bio_lab.sqlite")
    logger.info(f"Test workspace created: {workspace_dir} (sqlite DB copied)")
    return workspace_dir


def _build_test_config(workspace_dir: Path, config_file: str | Path = _DEFAULT_AGENT_CONFIG_FILE) -> Path:
    """Load config from YAML, resolve paths, override SEMANTIC_LAYER base_url, write to temp file."""
    import yaml

    config = _load_agent_config(config_file)
    config = _resolve_config_paths(config, workspace_dir)
    # 默认走内联 mock server（离线可复现）；CLI 可显式切到真实
    # semantic-service URL（可能是自签 https，跳过证书校验）。
    _apply_semantic_layer_config(config)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_bio_lab_", delete=False) as tmp:
        yaml.safe_dump(config, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        return Path(tmp.name)


def _cleanup_test_workspace(workspace_dir: Path, config_path: Path) -> None:
    """Remove temporary workspace directory and config file."""
    config_path.unlink(missing_ok=True)
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir, ignore_errors=True)
        logger.info(f"Cleaned up test workspace: {workspace_dir}")


async def run_tool_mode(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    config_file: str | Path = _DEFAULT_AGENT_CONFIG_FILE,
) -> None:
    """Start mock services and enter interactive terminal chat mode."""
    workspace_dir = _create_test_workspace()
    config_path = _build_test_config(workspace_dir, config_file=config_file)
    logger.info(f"Tool mode config written to: {config_path}")

    from dataagent.interface.cli.main import run_terminal_mode

    try:
        await run_terminal_mode(
            str(config_path),
            user_id=user_id,
            session_id=session_id,
        )
    finally:
        _cleanup_test_workspace(workspace_dir, config_path)


async def run_single_query(query: str, *, config_file: str | Path = _DEFAULT_AGENT_CONFIG_FILE) -> None:
    """Run a single user query against the agent and print the response."""
    workspace_dir = Path(tempfile.mkdtemp(prefix="bio_lab_query_"))
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "bio_lab.sqlite")

    config_path = _build_cache_test_config(
        workspace_dir,
        enable_human_feedback=False,
        config_file=config_file,
    )
    logger.info(f"Query mode config: {config_path}")

    from dataagent.interface.sdk.agent import DataAgent

    agent = DataAgent.from_config(str(config_path))
    try:
        response = await agent.chat(query, session_id=None)
        final = response if isinstance(response, str) else getattr(response, "content", str(response))
        print(f"\n{'=' * 60}")
        print(f"问题: {query}")
        print(f"{'=' * 60}")
        print(f"回答: {final}")
        print(f"{'=' * 60}\n")
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        config_path.unlink(missing_ok=True)
