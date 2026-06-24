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
"""E2E test for DataAgent with fully automatic mock services.

Mock services are started and stopped automatically when this test runs:

- **MetaVisor**: In-process HTTP server on port 31099 serving pre-cached JSON
  responses (captured from the real MetaVisor service). No external dependency.
- **OntologyEnv**: Python-level ``unittest.mock.patch`` returning fixture data.

Usage::

    python tests/e2e/changping/test_changping.py
    python tests/e2e/changping/test_changping.py --tool_mode
    python tests/e2e/changping/test_changping.py --tool_mode --user anonymous --session 20260622_080645_ec8a4895-8284-47ce-b6e6-e96165d99e09

No pre-starting of mock_metavisor_server.py is required.
"""

import asyncio
import atexit
import json
import os
import signal
import sys
import shutil
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="DEBUG", colorize=True, backtrace=True, diagnose=True)

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))
CHANGPING_DIR = Path(__file__).resolve().parent
CONFIG_DIR = CHANGPING_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_QWEN_CACHE_ANCHOR", "1")

# ---------------------------------------------------------------------------
# Inline MetaVisor mock server (pre-cached offline responses)
# ---------------------------------------------------------------------------
_MOCK_PORT = 32000
_mock_server: HTTPServer | None = None


def _load_metavisor_cache() -> dict[str, Any]:
    """Load pre-captured MetaVisor responses from the merged config JSON."""
    cache_path = CONFIG_DIR / "metavisor_responses.json"
    with open(cache_path, encoding="utf-8") as fh:
        cache = json.load(fh)
    return cache


_MV_CACHE: dict[str, Any] | None = None


def _get_metavisor_cache() -> dict[str, Any]:
    global _MV_CACHE
    if _MV_CACHE is None:
        _MV_CACHE = _load_metavisor_cache()
        logger.info(f"Loaded {_MV_CACHE.__len__()} MetaVisor response(s) from config")
    return _MV_CACHE


class _MockMVHandler(BaseHTTPRequestHandler):
    """Serves pre-cached MetaVisor responses."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        cache = _get_metavisor_cache()

        if path == "/api/metaVisor/v3/advanced-search/table-list":
            self._json(cache.get("table-list") or {"error": "table-list not cached"})
            return
        if path == "/api/metaVisor/v3/advanced-search/table-columns-info":
            tname = qs.get("tableName", [""])[0]
            self._json(cache.get(f"columns:{tname}") or {"error": f"{tname} not cached"})
            return
        if path == "/api/metaVisor/v3/advanced-search/joinable-tables":
            self._json(cache.get("joinable-tables") or {"error": "joinable-tables not cached"})
            return
        if path in (
            "/api/metaVisor/v3/advanced-search/semantic-search-columns",
            "/api/metaVisor/v3/advanced-search/vector-search-table-desc",
            "/api/metaVisor/v3/advanced-search/semantic-search-tables",
        ):
            key = path.split("/")[-1]
            self._json(cache.get(key, {}))
            return
        self._error(404, f"Unknown endpoint: {path}")

    def _json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200 if isinstance(data, dict) and "error" not in data else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass


def _start_mock_metavisor() -> None:
    """Start the inline MetaVisor mock HTTP server in a daemon thread."""
    global _mock_server
    _mock_server = HTTPServer(("127.0.0.1", _MOCK_PORT), _MockMVHandler)
    t = threading.Thread(target=_mock_server.serve_forever, daemon=True)
    t.start()
    import socket
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", _MOCK_PORT), timeout=0.5):
                logger.info(f"MetaVisor mock HTTP server listening on http://127.0.0.1:{_MOCK_PORT}")
                return
        except OSError:
            import time
            time.sleep(0.2)
    raise RuntimeError(f"Mock MetaVisor server failed to start on port {_MOCK_PORT}")


def _stop_mock_metavisor() -> None:
    """Stop the inline MetaVisor mock HTTP server."""
    global _mock_server
    if _mock_server:
        _mock_server.shutdown()
        _mock_server = None


# ---------------------------------------------------------------------------
# OntologyEnv mock
# ---------------------------------------------------------------------------
def _load_ontology_fixture() -> dict[str, str]:
    """Return the formatted ontology description from changping02_ontology.json."""
    spec_path = CONFIG_DIR / "changping02_ontology.json"
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    ontology_data = data.get("changping02", data)
    if isinstance(ontology_data, dict) and "changping02" in data:
        ontology_data = data["changping02"]

    entities = ontology_data.get("entities", [])
    relations = ontology_data.get("relations", [])
    object_types = [e.get("display_name", e.get("api_name", "")) for e in entities]
    object_type_details = []
    for e in entities:
        name = e.get("display_name", e.get("api_name", ""))
        props = [{"property_name": p.get("display_name"), "property_description": p.get("description")} for p in e.get("properties", [])]
        object_type_details.append({"entity_name": name, "entity_description": e.get("description", ""), "properties": props})

    relation_triplets = []
    for r in relations:
        relation_triplets.append({"source": r.get("source_entity_type"), "relation": r.get("display_name"),
                                  "target": r.get("target_entity_type"), "cardinality": r.get("cardinality"),
                                  "description": r.get("description", "")})

    def _pretty(obj: list[dict]) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    return {
        "original_msg": f"\n对本体查询结果如下：\n本体目前包含以下几种类型实体：\n{_pretty(object_types)}\n\n每种实体的描述和属性定义如下:\n{_pretty(object_type_details)}\n\n实体之间有以下几种类型的关联，每种关联用(源实体-关系-目标实体)的三元组表示:\n{_pretty(relation_triplets)}\n\n可以根据以上信息理解实体间的关联关系，以及每个实体的属性含义，从而构造查询条件。\n",
        "frontend_msg": f"已从本地spec文件加载本体描述信息，本体中共包括{len(object_types)}种实体，{len(relation_triplets)}种关系，它们的具体schema也已经被加载。",
    }


@contextmanager
def mock_ontology_env():
    """Patch OntologyEnv.get_ontology_description to return fixture data."""
    result = _load_ontology_fixture()
    logger.info("OntologyEnv.get_ontology_description() → mocked (config/changping02_ontology.json)")

    with patch("dataagent.actions.gym.ontology_env.OntologyEnv.get_ontology_description", lambda self: result):
        yield


# ---------------------------------------------------------------------------
# Test config builder
# ---------------------------------------------------------------------------
def _resolve_path(value: str, base: Path) -> str:
    """Resolve a relative path to absolute using *base* as root."""
    p = Path(value)
    if p.is_absolute():
        return value
    return str((base / p).resolve())


def _resolve_config_paths(config: dict, workspace_dir: Path) -> dict:
    """Resolve relative paths and placeholders in known config fields."""
    resolved = config.copy()

    workspace = resolved.get("WORKSPACE", {})
    if isinstance(workspace, dict):
        path_val = workspace.get("path", "")
        if path_val == "__WORKSPACE_DIR__":
            workspace["path"] = str(workspace_dir)
        allow_path = workspace.get("allow_path", [])
        if isinstance(allow_path, list):
            workspace["allow_path"] = [
                str(workspace_dir) if p == "__WORKSPACE_DIR__" else _resolve_path(p, CHANGPING_DIR)
                for p in allow_path
            ]

    database = resolved.get("DATABASE", {})
    if isinstance(database, dict):
        db_config = database.get("config", {})
        if isinstance(db_config, dict) and "path" in db_config:
            path_val = db_config["path"]
            if "__WORKSPACE_DIR__" in path_val:
                db_config["path"] = path_val.replace("__WORKSPACE_DIR__", str(workspace_dir))

    tools = resolved.get("TOOLS", {})
    if isinstance(tools, dict):
        skills = tools.get("skills", {})
        if isinstance(skills, dict):
            custom_dirs = skills.get("custom_dirs", [])
            if isinstance(custom_dirs, list):
                skills["custom_dirs"] = [_resolve_path(d, CHANGPING_DIR) for d in custom_dirs]

    return resolved


_ORIGINAL_SQLITE_PATH = CHANGPING_DIR / "data" / "changping02.sqlite"


def _create_test_workspace() -> Path:
    """Create a temporary workspace directory and copy the sqlite DB into it.

    The copy ensures the original DB is never modified by test runs.
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_changping_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")
    logger.info(f"Test workspace created: {workspace_dir} (sqlite DB copied)")
    return workspace_dir


def _build_test_config(workspace_dir: Path) -> Path:
    """Load config from YAML, resolve paths/placeholders, override METAVISOR URL, write to temp file."""
    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = _resolve_config_paths(config, workspace_dir)

    config.setdefault("METAVISOR", {})["metavisor_url"] = f"http://localhost:{_MOCK_PORT}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_changping_", delete=False) as tmp:
        yaml.safe_dump(config, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        return Path(tmp.name)


# ---------------------------------------------------------------------------
# Automated human feedback with HITL assertion
# ---------------------------------------------------------------------------
_FEEDBACK_RESPONSES: list[str] = []
_FEEDBACK_INDEX = 0
_HITL_TRIGGERED = False


def _auto_input(prompt: str) -> str:
    """Mock input() that returns pre-configured feedback responses and records HITL trigger."""
    global _FEEDBACK_INDEX, _HITL_TRIGGERED
    _HITL_TRIGGERED = True
    if _FEEDBACK_INDEX < len(_FEEDBACK_RESPONSES):
        response = _FEEDBACK_RESPONSES[_FEEDBACK_INDEX]
        _FEEDBACK_INDEX += 1
        logger.info(f"[Auto HITL] Prompt: {prompt.strip()!r} → Response: {response!r}")
        return response
    logger.warning(f"[Auto HITL] No more configured responses, returning empty string. Prompt: {prompt.strip()!r}")
    return ""


@contextmanager
def auto_human_feedback(responses: list[str]):
    """Patch builtins.input to automatically provide human feedback responses.

    Records whether HITL was triggered (i.e. input() was called at least once).

    Args:
        responses: Ordered list of feedback strings to return for each HITL prompt.
    """
    global _FEEDBACK_INDEX, _HITL_TRIGGERED, _FEEDBACK_RESPONSES
    _FEEDBACK_RESPONSES = responses
    _FEEDBACK_INDEX = 0
    _HITL_TRIGGERED = False
    logger.info(f"[Auto HITL] Configured {len(responses)} feedback response(s): {responses}")

    with patch("builtins.input", _auto_input):
        yield


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
async def _run_test(query: str, feedback_responses: list[str] | None = None) -> dict:
    """Run a single test query with optional auto human feedback.

    Returns:
        dict with keys: 'response' (agent final state), 'hitl_triggered' (bool),
        'workspace_dir' (Path), 'config_path' (Path).
    """
    workspace_dir = _create_test_workspace()
    config_path = _build_test_config(workspace_dir)
    logger.info(f"Test config written to: {config_path}")

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path)

    hitl_triggered = False
    if feedback_responses:
        with auto_human_feedback(feedback_responses):
            response = await agent.chat(query, session_id=None)
        hitl_triggered = _HITL_TRIGGERED
    else:
        response = await agent.chat(query, session_id=None)

    return {"response": response, "hitl_triggered": hitl_triggered, "workspace_dir": workspace_dir, "config_path": config_path}


async def test_create_neutralization_experiment():
    """Test: create neutralization experiment — verifies HITL is triggered and auto-confirmed."""
    query = "帮我创建BD55-1111抗体和XBB.1.5病毒的中和实验（使用huh-7细胞）"
    feedback_responses = ["确认，请创建该中和实验"]
    result = await _run_test(query, feedback_responses=feedback_responses)

    assert result["hitl_triggered"], "HITL (human-in-the-loop) was NOT triggered — expected the agent to request human feedback before creating the experiment"
    logger.info(f"✅ HITL triggered: {result['hitl_triggered']}")
    logger.info(f"Response: {result['response']}")
    logger.info("test_create_neutralization_experiment PASSED.")

    _cleanup_test_workspace(result["workspace_dir"], result["config_path"])


async def test_nl2sql_query():
    """Test: nl2sql query for huh-7 cell and cell sample info."""
    query = "调用nl2sql查询名称为huh-7的细胞及其对应的细胞样本信息"
    result = await _run_test(query)

    logger.info(f"Response: {result['response']}")
    logger.info("test_nl2sql_query finished.")

    _cleanup_test_workspace(result["workspace_dir"], result["config_path"])


def _cleanup_test_workspace(workspace_dir: Path, config_path: Path) -> None:
    """Remove temporary workspace directory and config file."""
    config_path.unlink(missing_ok=True)
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir, ignore_errors=True)
        logger.info(f"Cleaned up test workspace: {workspace_dir}")


# ---------------------------------------------------------------------------
# Tool mode — interactive CLI with mock services
# ---------------------------------------------------------------------------
async def run_tool_mode(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Start mock services and enter interactive terminal chat mode.

    Uses the same workspace/config setup as test cases, but allows free
    user interaction via the dataagent CLI terminal loop.

    Args:
        user_id: 可选，用户 ID（默认 anonymous，由 run_terminal_mode 兜底）
        session_id: 可选，固定会话 ID（默认每进程生成 时间戳_uuid）
    """
    workspace_dir = _create_test_workspace()
    config_path = _build_test_config(workspace_dir)
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Changping e2e test / interactive tool")
    parser.add_argument(
        "--tool_mode",
        action="store_true",
        help="Interactive mode: start mock services and enter free terminal chat",
    )
    parser.add_argument(
        "--user",
        "-u",
        default=None,
        metavar="USER_ID",
        help="用户 ID（默认 anonymous，由 run_terminal_mode 兜底）",
    )
    parser.add_argument(
        "--session",
        "-s",
        default=None,
        metavar="SESSION_ID",
        help="会话 ID：默认本进程内生成 时间戳_uuid；指定则固定该会话 ID",
    )
    args = parser.parse_args()

    _start_mock_metavisor()
    try:
        with mock_ontology_env():
            if args.tool_mode:
                await run_tool_mode(user_id=args.user, session_id=args.session)
            else:
                await test_create_neutralization_experiment()
                logger.info("All tests finished.")
    finally:
        _stop_mock_metavisor()


if __name__ == "__main__":
    def _signal_handler(sig, frame):
        _stop_mock_metavisor()
        os._exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    atexit.register(_stop_mock_metavisor)
    asyncio.run(main())
