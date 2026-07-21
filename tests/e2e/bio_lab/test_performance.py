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

"""E2E test for bio_lab: main Agent cache hit rate optimization v3.0.

This test verifies the v3.0 cache optimization (D6: move runtime_environment
out of SystemMessage into VariableUser) by replaying the user's real
2026-06-22 session query sequence. The 7-query session is split across two
DataAgent processes to simulate a real session restart:

  process 0: Q1-Q3 (normal continuous conversation)
  process 1: Q4-Q7 (session resumed from disk after restart)

Key fixes verified by this test:
- D6: SystemMessage no longer contains dynamic CPU%/Memory% values, so bp 1
  (System) cache prefix stays byte-stable across process restarts.
- D8 (revised): session_history_restore now loads the full messages.json
  history on restart (max_history_messages folding was removed). Session
  compression is delegated to the pruner hook.
- D2.1: DATAAGENT_CACHE_BREAKPOINT_ANNOTATION=1 enables bp position
  annotation in context_dump for offline inspection.

Design principles (per user request):
- No time-based assertions (queries can take arbitrarily long).
- Do NOT delete intermediate logs, context dumps, trajectories, or workspace
  artifacts — they are preserved for offline analysis.
- Cache hit rate assertions are placed at the END of the test, after all
  queries have completed, so the test runs to completion before any failure.

Usage::

    export DATAAGENT_CACHE_ANCHOR=1
    export DATAAGENT_CONTEXT_DUMP=1
    export DATAAGENT_CACHE_BREAKPOINT_ANNOTATION=1
    python tests/e2e/bio_lab/test_performance.py
    python tests/e2e/bio_lab/test_performance.py --skip_slow

    # Switch model preset (default: deepseek). bailian enables cache_control.
    python tests/e2e/bio_lab/test_performance.py --model bailian

    # Override pruner / IR thresholds.
    python tests/e2e/bio_lab/test_performance.py \\
        --compress_message_cnt 100 --recent_turns 50

    # Re-extract from a prior run.
    python tests/e2e/bio_lab/test_performance.py --tc2_only \\
        --user_id cache_test_user_v3_20260622_141023_ab12 \\
        --session_id cache_test_session_v3_20260622_141023_ab12

CLI options:
  --model {deepseek,openai,bailian}  Model preset for MODEL.chat_model.
                                 openai → openai/Qwen3.7-Plus via OPENAI_BASE_URL.
                                 bailian → bailian/Qwen3.7-Plus via DashScope (cache_control).
                                 deepseek → deepseek/deepseek-v4-flash.
  --compress_message_cnt N       CONTEXT.compress_message_cnt pruner threshold.
  --recent_turns N               CONTEXT.recent_turns IR threshold (0 = replace
                                 all turns).

Each run auto-generates a fresh timestamped user_id/session_id under
dataagent_home() (e.g. ``cache_test_user_v3_20260623_141023_ab12``), so
historical artifacts never collide and never need manual archiving.
"""

import asyncio
import json
import os
import random
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

BIO_LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BIO_LAB_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_CACHE_ANCHOR", "1")
os.environ.setdefault("DATAAGENT_CACHE_BREAKPOINT_ANNOTATION", "1")


def _disable_proxy_env() -> None:
    """Strip inherited proxy settings so the e2e test is self-contained.

    This avoids httpx/litellm picking up Clash Verge SOCKS/HTTP proxy settings
    from the parent shell when the test is launched via `uv run`.
    """
    for key in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "FTP_PROXY",
        "ftp_proxy",
        "NO_PROXY",
        "no_proxy",
        "SOCKS_PROXY",
        "socks_proxy",
        "SOCKS5_PROXY",
        "socks5_proxy",
    ):
        os.environ.pop(key, None)


_disable_proxy_env()

# ---------------------------------------------------------------------------
# Inline MetaVisor mock server (pre-cached offline responses)
# ---------------------------------------------------------------------------
_MOCK_PORT = 0  # 0 = auto-resolve (random or --mock_port); set before _start_mock_metavisor
# Semantic-service base URL. Ontology (get_ontology_description) and NL2SQL
# perceptor both go through SemanticServiceClient reading SEMANTIC_LAYER.base_url.
# Defaults to the inline mock server (offline-reproducible); set the
# SEMANTIC_SERVICE_URL env var to opt into a real semantic-service instance.
_SEMANTIC_SERVICE_URL = os.getenv("SEMANTIC_SERVICE_URL", "")
_CHANGPING_SCENE = os.getenv("CHANGPING_SCENE", "bio_lab")
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


class _ReusableHTTPServer(HTTPServer):
    """HTTPServer with SO_REUSEADDR so the port can be rebound immediately
    after the process exits, avoiding 'Address already in use' (TIME_WAIT)."""

    allow_reuse_address = True


class _MockMVHandler(BaseHTTPRequestHandler):
    """Serves pre-cached MetaVisor responses."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)
        cache = _get_metavisor_cache()

        if path == "/api/semantic/v1/advanced-search/table-list":
            self._json(cache.get("table-list") or {"error": "table-list not cached"})
            return
        if path == "/api/semantic/v1/advanced-search/table-columns-info":
            tname = qs.get("tableName", [""])[0]
            self._json(cache.get(f"columns:{tname}") or {"error": f"{tname} not cached"})
            return
        if path == "/api/semantic/v1/advanced-search/joinable-tables":
            self._json(cache.get("joinable-tables") or {"error": "joinable-tables not cached"})
            return
        if path in (
            "/api/semantic/v1/advanced-search/semantic-search-columns",
            "/api/semantic/v1/advanced-search/vector-search-table-desc",
            "/api/semantic/v1/advanced-search/semantic-search-tables",
        ):
            key = path.split("/")[-1]
            self._json(cache.get(key, {}))
            return
        self._error(404, f"Unknown endpoint: {path}")

    def _json(self, data: Any) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
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
    _mock_server = _ReusableHTTPServer(("127.0.0.1", _MOCK_PORT), _MockMVHandler)
    t = threading.Thread(target=_mock_server.serve_forever, daemon=True)
    t.start()
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", _MOCK_PORT), timeout=0.5):
                logger.info(f"MetaVisor mock HTTP server listening on http://127.0.0.1:{_MOCK_PORT}")
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"Mock MetaVisor server failed to start on port {_MOCK_PORT}")


def _stop_mock_metavisor() -> None:
    """Stop the inline MetaVisor mock HTTP server and release the socket fd."""
    global _mock_server
    if _mock_server is not None:
        try:
            _mock_server.shutdown()
            _mock_server.server_close()
        except Exception as exc:
            logger.warning(f"Failed to stop mock MetaVisor server cleanly: {exc}")
        finally:
            _mock_server = None


# Register atexit so the mock server is cleaned up even on ungraceful exits
# (e.g., SIGTERM, unhandled exceptions). Prevents TIME_WAIT "Address already
# in use" on subsequent runs.
import atexit  # noqa: E402

atexit.register(_stop_mock_metavisor)


# ---------------------------------------------------------------------------
# Ontology description mock
# ---------------------------------------------------------------------------
def _load_ontology_fixture() -> dict[str, str]:
    """Return ontology description rendered from the semantic-service mock cache."""
    from dataagent.actions.tools.semantic_tool.ontology_query import (
        _fetch_columns,
        _fetch_entities,
        _fetch_relations,
        _render_ontology_description,
    )

    class _FixtureSemanticClient:
        def __init__(self, cache: dict[str, Any]):
            self.cache = cache

        def get_table_list(self, database_name: str, *, limit: int) -> list:
            return list(self.cache.get("table-list") or [])[:limit]

        def get_table_columns_info(self, table_name: str, *, limit: int) -> dict:
            columns = self.cache.get(f"columns:{table_name}") or {}
            return dict(list(columns.items())[:limit])

        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list:
            del table_names
            return list(self.cache.get("joinable-tables") or [])[:limit]

    database = _CHANGPING_SCENE
    client = _FixtureSemanticClient(_get_metavisor_cache())
    entities = _fetch_entities(client, database)
    columns_by_table = {
        e["table_name"]: _fetch_columns(client, e["table_name"]) for e in entities if e.get("table_name")
    }
    relations = _fetch_relations(client, [e["table_name"] for e in entities if e.get("table_name")])
    return _render_ontology_description(entities, columns_by_table, relations, database, [database])


@contextmanager
def mock_ontology_description():
    """Patch semantic ontology description retrieval to return fixture data."""
    result = _load_ontology_fixture()
    logger.info("semantic ontology get_ontology_description() → mocked (config/metavisor_responses.json)")

    with patch(
        "dataagent.actions.tools.semantic_tool.ontology_query.get_ontology_description", lambda *, _tool_context: result
    ):
        yield


# ---------------------------------------------------------------------------
# Config path resolver
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
                str(workspace_dir) if p == "__WORKSPACE_DIR__" else _resolve_path(p, BIO_LAB_DIR) for p in allow_path
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
                skills["custom_dirs"] = [_resolve_path(d, BIO_LAB_DIR) for d in custom_dirs]

    return resolved


_ORIGINAL_SQLITE_PATH = BIO_LAB_DIR / "data" / "bio_lab.sqlite"


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
    if len(_FEEDBACK_RESPONSES) > _FEEDBACK_INDEX:
        response = _FEEDBACK_RESPONSES[_FEEDBACK_INDEX]
        _FEEDBACK_INDEX += 1
        logger.info(f"[Auto HITL] Prompt: {prompt.strip()!r} → Response: {response!r}")
        return response
    logger.warning(f"[Auto HITL] No more configured responses, returning empty string. Prompt: {prompt.strip()!r}")
    return ""


@contextmanager
def auto_human_feedback(responses: list[str]):
    """Patch builtins.input to automatically provide human feedback responses."""
    global _FEEDBACK_INDEX, _HITL_TRIGGERED, _FEEDBACK_RESPONSES
    _FEEDBACK_RESPONSES = responses
    _FEEDBACK_INDEX = 0
    _HITL_TRIGGERED = False
    logger.info(f"[Auto HITL] Configured {len(responses)} feedback response(s): {responses}")

    with patch("builtins.input", _auto_input):
        yield


# Each test run gets a fresh, timestamped user_id / session_id directory under
# dataagent_home(), so historical execution artifacts (logs, context dumps,
# trajectories, messages.json) never collide or require manual archiving
# between runs. Override via --user_id / --session_id CLI args (e.g. for
# --tc2_only re-extraction on a prior run).
_RUN_STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
_RUN_SUFFIX = secrets.token_hex(2)  # 4 hex chars, avoids same-second collisions
CACHE_TEST_USER_ID = f"cache_test_user_v3_{_RUN_STAMP}_{_RUN_SUFFIX}"
CACHE_TEST_SESSION_ID = f"cache_test_session_v3_{_RUN_STAMP}_{_RUN_SUFFIX}"

OVERALL_HIT_RATE_THRESHOLD = 45.0
# Post-creation threshold accounts for Q1 cold-start calls (no history_summary,
# bp1=System only ~2429 tokens (no StableUser) → iteration-first calls rely on bp3 tail_anchor). Q2-Q7
# (which have history_summary → bp1 ~7900-8500 tokens) average 81.5%.
POST_CREATION_HIT_RATE_THRESHOLD = 73.0
RESTART_FIRST_CALL_HIT_RATE_THRESHOLD = 20.0

CACHE_THRESHOLD_PROFILE = "optimized"  # "optimized" | "baseline" | "off"

# Model presets for the cache test. Each entry maps a `--model` CLI choice to
# the YAML MODEL.chat_model fields. `base_url_env` / `api_key_env` name the
# environment variables that the test resolves at config-build time so the
# generated YAML stays self-contained.
MODEL_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "provider": "openai",
        "model": "Qwen3.7-Plus",
        "base_url_env": "OPENAI_BASE_URL",
        "api_key_env": "OPENAI_API_KEY",
    },
    "bailian": {
        "provider": "bailian",
        "model": "qwen3.7-plus",
        "base_url_env": "BAILIAN_BASE_URL",
        "api_key_env": "BAILIAN_API_KEY",
    },
}
DEFAULT_MODEL_CHOICE = "deepseek"
DEFAULT_COMPRESS_MESSAGE_CNT = 200
DEFAULT_RECENT_TURNS = 200


def _resolve_model_preset(model_choice: str) -> dict[str, str]:
    """Return the model preset for ``model_choice`` or raise ValueError."""
    if model_choice not in MODEL_PRESETS:
        raise ValueError(f"Unknown model choice: {model_choice!r}. Supported: {sorted(MODEL_PRESETS)}")
    return dict(MODEL_PRESETS[model_choice])


def _apply_model_choice(config: dict, model_choice: str) -> dict:
    """Override config["MODEL"]["chat_model"] based on ``model_choice``.

    Writes ``base_url`` / ``api_key`` as ``$env{...}`` placeholders so the
    ConfigManager resolves them from ``.env`` at load time (consistent with
    the original ``main_config.yaml``).
    """
    preset = _resolve_model_preset(model_choice)
    model_cfg = config.setdefault("MODEL", {}).setdefault("chat_model", {})
    model_cfg["model_type"] = "chat"
    model_cfg["provider"] = preset["provider"]
    model_cfg.setdefault("params", {})
    model_cfg["params"]["model"] = preset["model"]
    model_cfg["params"]["base_url"] = f"$env{{{preset['base_url_env']}}}"
    model_cfg["params"]["api_key"] = f"$env{{{preset['api_key_env']}}}"
    model_cfg["params"].setdefault("temperature", 0)
    return config


def _build_cache_test_config(
    workspace_dir: Path,
    compress_message_cnt: int = DEFAULT_COMPRESS_MESSAGE_CNT,
    compress_token_limit: int = 128000,
    enable_human_feedback: bool = True,
    session_root: Path | None = None,
    model_choice: str = DEFAULT_MODEL_CHOICE,
    recent_turns: int | None = DEFAULT_RECENT_TURNS,
) -> Path:
    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = _resolve_config_paths(config, workspace_dir)
    # Ontology (get_ontology_description) and NL2SQL perceptor both go through
    # SemanticServiceClient reading SEMANTIC_LAYER.base_url. 默认走内联 mock
    # server（离线可复现，三个基础检索 REST 端点齐全）；仅当显式提供
    # SEMANTIC_SERVICE_URL 时才 opt-in 到真实 semantic-service。
    if _SEMANTIC_SERVICE_URL:
        config.setdefault("SEMANTIC_LAYER", {})["base_url"] = _SEMANTIC_SERVICE_URL
        # 真实服务可能是自签 https，跳过证书校验。
        config.setdefault("SEMANTIC_LAYER", {})["verify_ssl"] = False
    else:
        config.setdefault("SEMANTIC_LAYER", {})["base_url"] = f"http://localhost:{_MOCK_PORT}"
    # get_ontology_description_tool 走 SemanticServiceClient 的基础检索 REST 接口
    # (advanced-search/table-list | table-columns-info | joinable-tables),
    # 场景（databaseName）与其它 semantic 工具统一取自 DATABASE.db_id。
    config.setdefault("DATABASE", {})["db_id"] = _CHANGPING_SCENE

    context_cfg = config.setdefault("CONTEXT", {})
    context_cfg["compress_message_cnt"] = compress_message_cnt
    context_cfg["compress_token_limit"] = compress_token_limit
    if recent_turns is not None:
        context_cfg["recent_turns"] = recent_turns

    _apply_model_choice(config, model_choice)

    config.setdefault("AGENT_CONFIG", {})["enable_human_feedback"] = enable_human_feedback

    dump_dir = session_root if session_root is not None else workspace_dir
    dump_dir.mkdir(parents=True, exist_ok=True)
    out_path = dump_dir / "test_cache_v3.yaml"
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    return out_path


def _resolve_session_root() -> Path:
    from dataagent.utils.runtime_paths import dataagent_home

    return dataagent_home() / CACHE_TEST_USER_ID / CACHE_TEST_SESSION_ID


def _collect_usage_metadata_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    usage_list: list[dict[str, Any]] = []
    if isinstance(messages, list):
        for msg in messages:
            msg_type = getattr(msg, "type", None) or ""
            if msg_type == "ai":
                usage = getattr(msg, "usage_metadata", None) or {}
                if usage and usage.get("input_tokens", 0) > 0:
                    usage_list.append(
                        {
                            "input_tokens": usage.get("input_tokens", 0),
                            "input_cache_read_tokens": usage.get("input_cache_read_tokens", 0),
                            "input_cache_creation_tokens": usage.get("input_cache_creation_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        }
                    )
    return usage_list


def _collect_usage_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    messages = state.get("messages", []) or []
    return _collect_usage_metadata_from_messages(messages)


def _extract_final_assistant_text(messages: list[Any]) -> str:
    """Return the text content of the last assistant message to the user.

    The agent's "final answer" for a single chat() call is the last
    AIMessage in the returned message list whose ``content`` is a non-empty
    string and that does NOT carry tool_calls (i.e. it's the closing
    natural-language reply, not an intermediate reasoning/planning step).

    Returns an empty string if no such message is found (e.g. the agent
    crashed or every AIMessage was a tool-call step).
    """
    last_text = ""
    if not isinstance(messages, list):
        return last_text
    for msg in messages:
        msg_type = getattr(msg, "type", None) or ""
        if msg_type != "ai":
            continue
        # Skip AIMessages that are pure tool-call planning steps (no text)
        tool_calls = getattr(msg, "tool_calls", None) or []
        content = getattr(msg, "content", "")
        if tool_calls and (not isinstance(content, str) or not content.strip()):
            continue
        if isinstance(content, str) and content.strip():
            last_text = content
    return last_text


def _find_created_experiment_id(db_path: Path, today: str) -> int | None:
    """Query the bio_lab.sqlite DB for the experiment created by Q1.

    A "valid Q1 creation" is a row in ``neutralization_experiments`` joined
    with ``experiments`` that satisfies ALL of:
      - inhibitor_sample_id  = EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID
      - pseudovirus_sample_id = EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID
      - cell_sample_id       = EXPECTED_HUH7_CELL_SAMPLE_ID
      - experiments.status   = EXPECTED_NEW_EXPERIMENT_STATUS
      - experiments.start_date = today (the test run date)

    Returns the experiment id (int) if found, else None. The id is
    auto-generated by the agent's INSERT script (e.g. 902036 in the deepseek
    reference run) so it cannot be predicted ahead of time.
    """
    import sqlite3

    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            SELECT ne.id
            FROM neutralization_experiments ne
            JOIN experiments e ON ne.id = e.id
            WHERE ne.inhibitor_sample_id = ?
              AND ne.pseudovirus_sample_id = ?
              AND ne.cell_sample_id = ?
              AND e.status = ?
              AND e.start_date = ?
            ORDER BY ne.id DESC
            LIMIT 1
            """,
            (
                EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID,
                EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID,
                EXPECTED_HUH7_CELL_SAMPLE_ID,
                EXPECTED_NEW_EXPERIMENT_STATUS,
                today,
            ),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def _compute_cache_hit_rate(usage_records: list[dict[str, Any]]) -> dict[str, float]:
    total_input = sum(r["input_tokens"] for r in usage_records)
    total_output = sum(r.get("output_tokens", 0) for r in usage_records)
    total_cache_read = sum(r["input_cache_read_tokens"] for r in usage_records)
    total_cache_creation = sum(r["input_cache_creation_tokens"] for r in usage_records)

    if total_input == 0:
        return {
            "hit_rate": 0.0,
            "total_input": 0,
            "total_output": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "num_calls": 0,
            "per_call_rates": [],
        }

    per_call_rates = []
    for r in usage_records:
        rate = r["input_cache_read_tokens"] / max(r["input_tokens"], 1)
        per_call_rates.append(round(rate * 100, 1))

    return {
        "hit_rate": round(total_cache_read / total_input * 100, 1),
        "total_input": total_input,
        "total_output": total_output,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_creation,
        "num_calls": len(usage_records),
        "per_call_rates": per_call_rates,
    }


def _format_per_query_summary(per_query_usages: list[dict[str, Any]]) -> str:
    """Format per-query cache stats as a multi-line string for logs/assertions.

    For each user question, shows: number of LLM calls, end-to-end execution
    time, overall hit rate, and input/output/cache_read/cache_creation token
    totals — in addition to the per-LLM-call rates that are already printed
    elsewhere.
    """
    if not per_query_usages:
        return "Per-query breakdown: (no queries)"
    lines = [f"Per-query breakdown ({len(per_query_usages)} queries):"]
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_calls = 0
    total_elapsed = 0.0
    for i, q in enumerate(per_query_usages, 1):
        s = _compute_cache_hit_rate(q["usages"])
        elapsed = q.get("elapsed_sec", 0)
        total_input += s["total_input"]
        total_output += s["total_output"]
        total_cache_read += s["cache_read"]
        total_cache_creation += s["cache_creation"]
        total_calls += s["num_calls"]
        total_elapsed += elapsed
        lines.append(
            f"  [{i}/{len(per_query_usages)}] {q['query_key']}: "
            f"calls={s['num_calls']}, elapsed={elapsed}s, hit_rate={s['hit_rate']}%, "
            f"input={s['total_input']}, output={s['total_output']}, "
            f"cache_read={s['cache_read']}, cache_creation={s['cache_creation']}"
        )
    overall_hit = round(total_cache_read / total_input * 100, 1) if total_input else 0.0
    lines.append(
        f"  [TOTAL] calls={total_calls}, elapsed={round(total_elapsed, 2)}s, "
        f"hit_rate={overall_hit}%, "
        f"input={total_input}, output={total_output}, "
        f"cache_read={total_cache_read}, cache_creation={total_cache_creation}"
    )
    return "\n".join(lines)


def _dump_cache_analysis(
    usage_records: list[dict[str, Any]],
    output_dir: Path,
    label: str = "",
) -> Path:
    analysis = {
        "label": label,
        "cache_stats": _compute_cache_hit_rate(usage_records),
        "per_call": usage_records,
    }
    suffix = f"_{label}" if label else ""
    out_path = output_dir / f"cache_analysis_v3{suffix}.json"
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cache analysis written to {out_path}")
    return out_path


def _extract_cache_from_messages_file(session_root: Path) -> dict[str, Any]:
    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    if not messages_file.exists():
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "num_calls": 0}

    from dataagent.core.context.message_history import read_messages_file

    try:
        records = read_messages_file(messages_file)
        usage_list = _collect_usage_metadata_from_messages(records)
        return _compute_cache_hit_rate(usage_list)
    except Exception as e:
        logger.warning(f"Failed to read messages.json: {e}")
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "num_calls": 0}


def _analyze_context_dumps(session_root: Path) -> list[dict[str, Any]]:
    dump_base = session_root / "workspace" / ".memory" / "context_dump"
    if not dump_base.exists():
        logger.warning(f"No context_dump directory at {dump_base}")
        return []

    analysis = []
    for run_dir in sorted(dump_base.iterdir()):
        if not run_dir.is_dir():
            continue
        for round_file in sorted(run_dir.iterdir()):
            if round_file.suffix != ".txt":
                continue
            content = round_file.read_text(encoding="utf-8")
            msg_count = content.count("--- [")
            analysis.append(
                {
                    "file": str(round_file.relative_to(dump_base)),
                    "message_count": msg_count,
                    "file_size": len(content),
                }
            )
    return analysis


def _verify_system_message_stability(session_root: Path) -> dict[str, Any]:
    """Verify D6 fix: SystemMessage should NOT contain CPU:/Memory: lines.

    Across all context dumps, the SYSTEM section should be byte-identical
    (no dynamic runtime_environment values).
    """
    import re

    dump_base = session_root / "workspace" / ".memory" / "context_dump"
    if not dump_base.exists():
        return {"stable": False, "reason": "no context_dump dir", "system_samples": []}

    system_samples: list[dict[str, Any]] = []
    system_texts: list[str] = []
    for run_dir in sorted(dump_base.iterdir()):
        if not run_dir.is_dir():
            continue
        round_files = sorted(run_dir.glob("round_*.txt"))
        if not round_files:
            continue
        # Only inspect round_0 of each run (the first call after restart)
        round_file = round_files[0]
        content = round_file.read_text(encoding="utf-8")

        # Extract SYSTEM section. The header may include breakpoint annotation
        # like "--- [0] SYSTEM [bp 1 candidate] ---", so use regex to match.
        lines = content.splitlines()
        sys_lines: list[str] = []
        in_system = False
        for line in lines:
            if re.match(r"^--- \[0\] SYSTEM.*---", line):
                in_system = True
                continue
            if in_system and re.match(r"^--- \[\d+\].*---", line):
                break
            if in_system:
                sys_lines.append(line)

        system_text = "\n".join(sys_lines)
        has_cpu = any(line.strip().startswith("- CPU:") for line in sys_lines)
        has_memory = any(line.strip().startswith("- Memory:") for line in sys_lines)
        system_samples.append(
            {
                "file": str(round_file.relative_to(dump_base)),
                "system_chars": len(system_text),
                "has_cpu_line": has_cpu,
                "has_memory_line": has_memory,
                "system_hash": hash(system_text),
            }
        )
        system_texts.append(system_text)

    if not system_samples:
        return {"stable": False, "reason": "no round_0 files found", "system_samples": []}

    all_stable = all(not s["has_cpu_line"] and not s["has_memory_line"] for s in system_samples)
    # Also verify byte-stability: all system_texts should be identical
    byte_stable = len(set(system_texts)) == 1 if system_texts else False
    reason = (
        f"D6 verified: no CPU/Memory lines in SYSTEM, byte-stable across {len(system_samples)} runs"
        if all_stable and byte_stable
        else f"D6 FAILED: has_cpu_lines={[s['has_cpu_line'] for s in system_samples]}, byte_stable={byte_stable}"
    )
    return {
        "stable": all_stable and byte_stable,
        "reason": reason,
        "system_samples": system_samples,
    }


QUERY_SEQUENCES = {
    "create_experiment": {
        "query": "帮我创建BD55-1111抗体和XBB.1.5病毒的中和实验（使用huh-7细胞）",
        "feedback_responses": ["确认，请创建该中和实验"],
        "needs_feedback": True,
    },
    "find_antibody_neutralization": {
        "query": "查一下 帮我找出抗体 'BD-368' 能有效中和（IC50小于0.1）的所有假病毒名称",
        "needs_feedback": False,
    },
    "find_recent_experiment": {
        "query": "XBB.1.5和BD55-1111的中和实验，最近一次的实验编号是多少",
        "needs_feedback": False,
    },
    "count_cells": {
        "query": "有多少个不同类型的细胞，ID分别是什么",
        "needs_feedback": False,
    },
    "count_viruses": {
        "query": "有多少个不同类型的病毒，ID分别是什么",
        "needs_feedback": False,
    },
    "count_antibodies": {
        "query": "一共有多少个不同类型的抗体，ID是多少",
        "needs_feedback": False,
    },
    "ask_recent_experiment_id": {
        "query": "刚才创建的实验实验编号是多少",
        "needs_feedback": False,
    },
}


SLOW_QUERY_KEYS = ["create_experiment"]
FAST_QUERY_KEYS = [
    "find_antibody_neutralization",
    "find_recent_experiment",
    "count_cells",
    "count_viruses",
    "count_antibodies",
    "ask_recent_experiment_id",
]
# Subset of queries that are very fast (single nl2sql call, no multi-step exploration).
# Used by --quick for CI smoke testing (~5 min total).
QUICK_QUERY_KEYS = ["count_cells", "count_viruses", "count_antibodies"]

# Two-process session replay: Q1-Q3 run in process 0 (normal continuous
# conversation), Q4-Q7 run in process 1 (simulates process restart that
# resumes the same session from disk). This mirrors the real-world scenario
# where a long-running session survives an agent restart.
#
#   process 0 (no restart):  create_experiment → find_antibody_neutralization → find_recent_experiment
#   process 1 (restart):     count_cells → count_viruses → count_antibodies → ask_recent_experiment_id
#
# Only the first query of process 1 is a true "restart first call" — its
# cache hit rate measures whether bp1 (System) stays byte-stable
# across process restarts (the D6 metric). Within-process queries rely on
# bp3 (tail_anchor) for high hit rates and are NOT restarts.
PROCESS_0_KEYS = [
    "create_experiment",
    "find_antibody_neutralization",
    "find_recent_experiment",
]
PROCESS_1_KEYS = [
    "count_cells",
    "count_viruses",
    "count_antibodies",
    "ask_recent_experiment_id",
]

# =============================================================================
# Expected functional results (per-query correctness assertions)
# =============================================================================
# These constants define the expected agent output for each query. They are
# used by the per-query result assertions added to test_v3_session_replay() to
# catch functional regressions (e.g. NL2SQL generating wrong SQL that prevents
# the experiment from being created).
#
# Values are verified directly against tests/e2e/bio_lab/data/bio_lab.sqlite
# and cross-checked against a known-good deepseek run
# (cache_test_user_v3_20260630_163422_9a55, which created experiment 902036).
#
# Q1 (create_experiment): the agent should INSERT a new neutralization
# experiment into the DB. The new row in neutralization_experiments (joined
# with experiments) must satisfy:
#   - inhibitor_sample_id = 11396  (antibody sample whose antibody_id=11397,
#     whose proteins.name='BD55-1111')
#   - pseudovirus_sample_id = 904036  (sample whose pseudovirus_id=1000401,
#     whose pseudoviruses.name='XBB.1.5')
#   - cell_sample_id = 800001  (sample whose cell_id=800002, cells.name='huh-7')
#   - experiments.status = 'NEW'
#   - experiments.start_date = today (UTC date of the test run)
# The new experiment id is auto-generated by the agent's INSERT script; it
# cannot be predicted ahead of time, so we capture it from the DB after Q1
# and use it for Q3 / Q7 consistency assertions.
EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID = 11396
EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID = 904036
EXPECTED_HUH7_CELL_SAMPLE_ID = 800001
EXPECTED_NEW_EXPERIMENT_STATUS = "NEW"

# Q2 (find_antibody_neutralization): BD-368 effectively neutralizes (IC50 < 0.1
# AND fit_success=1) these 5 pseudoviruses. Verified against the DB:
#   SELECT DISTINCT pv.name FROM ... WHERE proteins.name='BD-368'
#   AND nifd.fit_success=1 AND nifd.ic50 < 0.1;
EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES = {
    "EG.5",
    "JN.1",
    "HK.3",
    "BA.2.86",
    "KP.2",
}

# Q4-Q6 (count queries): the queries now explicitly say "有多少个不同类型的X"
# ("how many DIFFERENT TYPES of X"), so the expected answer is the total count
# of distinct entities in the DB. Verified directly:
#   SELECT COUNT(*) FROM cells;            -> 2   (800002 huh-7, 900300 HEK293T-ACE2)
#   SELECT COUNT(*) FROM pseudoviruses;    -> 7
#   SELECT COUNT(*) FROM antibodies;       -> 8
EXPECTED_CELL_COUNT = 2
EXPECTED_CELL_IDS = {"800002", "900300"}
EXPECTED_PSEUDOVIRUS_COUNT = 7
EXPECTED_PSEUDOVIRUS_IDS = {
    "900400",
    "900401",
    "901401",
    "901402",
    "901403",
    "901404",
    "1000401",
}
EXPECTED_ANTIBODY_COUNT = 8
EXPECTED_ANTIBODY_IDS = {
    "11397",
    "900500",
    "900501",
    "900502",
    "901500",
    "901501",
    "901502",
    "901503",
}


async def test_v3_session_replay(
    skip_slow: bool = False,
    quick: bool = False,
    model_choice: str = DEFAULT_MODEL_CHOICE,
    compress_message_cnt: int = DEFAULT_COMPRESS_MESSAGE_CNT,
    recent_turns: int | None = DEFAULT_RECENT_TURNS,
) -> dict[str, Any]:
    """TC1: Replay the user's real 2026-06-22 session query sequence.

    Two-process session replay that mirrors the real-world scenario of a
    long-running session surviving an agent restart:

      process 0 (no restart):   Q1 create_experiment → Q2 find_antibody_neutralization → Q3 find_recent_experiment
      process 1 (restart):      Q4 count_cells → Q5 count_viruses → Q6 count_antibodies → Q7 ask_recent_experiment_id

    - Process 0: a single DataAgent instance handles Q1-Q3 consecutively.
      In-memory state (messages, plan, cache) carries over between queries.
      bp3 (tail_anchor) should provide high hit rates within this process.

    - Process 1: a fresh DataAgent instance resumes the same session from
      disk (session_history_restore loads the full messages.json history;
      session compression is delegated to the pruner hook).
      Only Q4's first LLM call is a true "restart first call" — its cache
      hit rate measures whether bp1 (System) stays byte-stable
      across process restarts (the D6 metric). Q5-Q7 then run consecutively
      within process 1, relying on bp3 again.

    Artifacts (logs, context dumps, trajectories) are preserved — no cleanup.
    Cache hit rate assertions are deferred to the end of the test.

    Args:
        skip_slow: If True, skip the "create experiment" query (takes ~10 min).
        quick: If True, run only 3 fast count queries in a single process
            (~5 min total, CI smoke test — no restart, no D6 verification).
        model_choice: Model preset for the test config — one of
            ``{"deepseek", "openai", "bailian"}`` (see :data:`MODEL_PRESETS`).
        compress_message_cnt: ``CONTEXT.compress_message_cnt`` threshold for
            the pruner hook (message-count based compression trigger).
        recent_turns: ``CONTEXT.recent_turns`` IR-replacement threshold
            (0 = replace all turns).

    Returns:
        dict with usage stats, per-query stats, and verification results.
    """

    session_root = _resolve_session_root()
    # Each run uses a timestamp-suffixed user_id/session_id, so this directory
    # is fresh by construction. The rmtree below is a defensive guard for the
    # rare case of same-second collisions or an explicit --user_id/--session_id
    # override pointing at a pre-existing dir. Artifacts from THIS run are
    # preserved after the test completes (no automatic cleanup).
    memory_dir = session_root / "workspace" / ".memory"
    if memory_dir.exists():
        shutil.rmtree(memory_dir, ignore_errors=True)
        logger.info(f"TC1 cleaned up pre-existing .memory dir: {memory_dir}")
    session_root.mkdir(parents=True, exist_ok=True)

    workspace_dir = session_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "bio_lab.sqlite")

    config_path = _build_cache_test_config(
        workspace_dir,
        compress_message_cnt=compress_message_cnt,
        enable_human_feedback=True,
        session_root=session_root,
        model_choice=model_choice,
        recent_turns=recent_turns,
    )
    logger.info(f"TC1 config: {config_path}")
    logger.info(
        f"TC1 model_choice={model_choice}, compress_message_cnt={compress_message_cnt}, recent_turns={recent_turns}"
    )
    logger.info(f"TC1 workspace: {workspace_dir} (preserved after test)")

    logger.info(f"TC1 session_root (fresh per run): {session_root}")
    logger.info(f"TC1 user_id={CACHE_TEST_USER_ID}")
    logger.info(f"TC1 session_id={CACHE_TEST_SESSION_ID}")

    if quick:
        query_keys = list(QUICK_QUERY_KEYS)
    else:
        query_keys = list(QUERY_SEQUENCES.keys())
        if skip_slow:
            query_keys = [k for k in query_keys if k not in SLOW_QUERY_KEYS]
    logger.info(f"TC1 query sequence ({len(query_keys)} queries): {query_keys}")

    all_usage: list[dict[str, Any]] = []
    per_query_usages: list[dict[str, Any]] = []
    per_query_responses: list[dict[str, Any]] = []

    # Two-process replay: reuse DataAgent instances within each process.
    # process_agents[0] serves Q1-Q3, process_agents[1] serves Q4-Q7.
    # Only the first query of process 1 is a true restart (session resumed
    # from disk); within-process queries reuse in-memory state.
    process_agents: dict[int, Any] = {}
    # Per-process snapshot of len(response["messages"]) after each chat() call.
    # Within a process the DataAgent instance is reused, so response["messages"]
    # accumulates across queries — slicing by this offset isolates the AIMessage
    # usage_metadata produced by the current query only (fix §1.5 per-query
    # cumulative stats bug).
    prev_msgs_len_by_proc: dict[int, int] = {}
    restart_query_indices: list[int] = []  # execution-order indices of restart queries

    for i, query_key in enumerate(query_keys):
        spec = QUERY_SEQUENCES[query_key]
        query = spec["query"]
        logger.info("=" * 60)
        logger.info(f"[TC1 Query {i + 1}/{len(query_keys)}] key={query_key}")
        logger.info(f"  query={query!r}")
        logger.info(f"  needs_feedback={spec.get('needs_feedback', False)}")

        # Determine process index: quick mode always uses process 0 (no
        # restart); full mode splits at PROCESS_0_KEYS / PROCESS_1_KEYS
        # boundary so Q1-Q3 share one DataAgent, Q4-Q7 share another.
        process_idx = 0 if quick else 0 if query_key in PROCESS_0_KEYS else 1

        # Create a new DataAgent instance only at process boundaries.
        # Within a process, the same instance is reused so in-memory
        # state (messages, plan, cache) carries over between queries.
        if process_idx not in process_agents:
            from dataagent.interface.sdk.agent import DataAgent

            process_agents[process_idx] = DataAgent.from_config(config_path)
            if process_idx > 0:
                # Only process 1+ is a true "restart" — it resumes an existing
                # session from disk. Process 0 is a cold start (new session,
                # no prior cache), so it is NOT counted as a restart.
                restart_query_indices.append(i)
                # session_history_restore loads prior queries' messages from
                # messages.json into state["messages"] inside chat(). Those
                # restored messages carry STALE usage_metadata (from their
                # original generation in a prior process). To avoid
                # double-counting them in per-query stats, initialize
                # prev_msgs_len to the persisted message count so the slice
                # only captures NEW messages produced by this chat() call.
                _messages_file = session_root / "workspace" / ".memory" / "messages.json"
                if _messages_file.exists():
                    try:
                        from dataagent.core.context.message_history import read_messages_file

                        _restored_count = len(read_messages_file(_messages_file))
                        prev_msgs_len_by_proc[process_idx] = _restored_count
                        logger.info(
                            f"  [Process {process_idx}] Session resumed from disk (restart); "
                            f"{_restored_count} restored messages excluded from per-query usage"
                        )
                    except Exception as e:
                        logger.warning(f"  [Process {process_idx}] Failed to read messages.json baseline: {e}")
                        logger.info(f"  [Process {process_idx}] Session resumed from disk (restart)")
                else:
                    logger.info(f"  [Process {process_idx}] Session resumed from disk (restart)")
            else:
                logger.info(f"  [Process {process_idx}] New DataAgent instance created")

        agent = process_agents[process_idx]

        initial_state = {
            "user_id": CACHE_TEST_USER_ID,
            "run_id": i,
        }
        # max_history_messages folding was removed; session_history_restore
        # now loads the full messages.json history on restart. Within-process
        # queries carry in-memory state, so restore is a no-op for them.

        # Snapshot the message count BEFORE chat() so we can slice off only
        # the messages produced by this chat() call (the agent instance is
        # reused within a process, so response["messages"] is cumulative).
        pre_msgs_len = prev_msgs_len_by_proc.get(process_idx, 0)

        # Time the end-to-end execution of this single user question
        # (from agent.chat() entry to its return). Covers Planner loop,
        # tool execution, and any human-feedback round-trips.
        t_start = time.perf_counter()
        if spec.get("needs_feedback") and spec.get("feedback_responses"):
            with auto_human_feedback(spec["feedback_responses"]):
                response = await agent.chat(
                    query,
                    session_id=CACHE_TEST_SESSION_ID,
                    initial_state=initial_state,
                )
        else:
            response = await agent.chat(
                query,
                session_id=CACHE_TEST_SESSION_ID,
                initial_state=initial_state,
            )
        t_end = time.perf_counter()
        elapsed_sec = round(t_end - t_start, 2)

        all_msgs = response.get("messages", []) or []
        # History should never shrink between calls within a process; if it
        # does (unexpected), fall back to the full list rather than silently
        # dropping usage data.
        new_msgs = all_msgs[pre_msgs_len:] if pre_msgs_len <= len(all_msgs) else all_msgs
        usages = _collect_usage_metadata_from_messages(new_msgs)
        prev_msgs_len_by_proc[process_idx] = len(all_msgs)
        all_usage.extend(usages)
        per_query_usages.append(
            {
                "query_key": query_key,
                "query": query,
                "usages": usages,
                "elapsed_sec": elapsed_sec,
                "process_idx": process_idx,
                "is_restart": i in restart_query_indices,
            }
        )

        # Capture summary without storing the full state (could be huge)
        msgs = response.get("messages", []) or []
        final_answer = _extract_final_assistant_text(msgs)
        per_query_responses.append(
            {
                "query_key": query_key,
                "num_messages": len(msgs),
                "num_llm_calls": len(usages),
                "elapsed_sec": elapsed_sec,
                "final_answer": final_answer,
            }
        )
        logger.info(
            f"  Captured {len(usages)} Planner LLM calls, {len(msgs)} messages "
            f"in {elapsed_sec}s (process={process_idx})"
        )
        if final_answer:
            preview = final_answer[:200].replace("\n", " ")
            logger.info(f"  Final answer preview: {preview}{'...' if len(final_answer) > 200 else ''}")
        else:
            logger.warning(f"  No final assistant text captured for query '{query_key}'")

    # ------------------------------------------------------------------
    # Aggregate statistics
    # ------------------------------------------------------------------
    overall_stats = _compute_cache_hit_rate(all_usage)
    post_creation_usage = [u for u in all_usage if u["input_cache_read_tokens"] > 0]
    post_creation_stats = (
        _compute_cache_hit_rate(post_creation_usage) if post_creation_usage else {"hit_rate": 0.0, "num_calls": 0}
    )

    logger.info("=" * 60)
    logger.info("TC1 Results:")
    logger.info(f"  Total LLM calls: {overall_stats['num_calls']}")
    logger.info(f"  Total input tokens: {overall_stats['total_input']}")
    logger.info(f"  Total output tokens: {overall_stats['total_output']}")
    logger.info(f"  Cache read tokens: {overall_stats['cache_read']}")
    logger.info(f"  Cache creation tokens: {overall_stats['cache_creation']}")
    logger.info(f"  Overall hit rate: {overall_stats['hit_rate']}%")
    logger.info(
        f"  Post-creation hit rate: {post_creation_stats['hit_rate']}% ({post_creation_stats['num_calls']} calls)"
    )
    logger.info(f"  Per-call rates: {overall_stats['per_call_rates']}")
    logger.info("  Per-query breakdown:")
    for i, q in enumerate(per_query_usages, 1):
        q_stats = _compute_cache_hit_rate(q["usages"])
        restart_tag = " [RESTART]" if q.get("is_restart") else ""
        logger.info(
            f"  [{i}/{len(per_query_usages)}] Query '{q['query_key']}'{restart_tag}: "
            f"calls={q_stats['num_calls']}, hit_rate={q_stats['hit_rate']}%, "
            f"input={q_stats['total_input']}, output={q_stats['total_output']}, "
            f"cache_read={q_stats['cache_read']}, cache_creation={q_stats['cache_creation']}, "
            f"elapsed={q.get('elapsed_sec', 0)}s, "
            f"per_call={q_stats['per_call_rates']}"
        )
    logger.info("=" * 60)

    # ------------------------------------------------------------------
    # Verify D6 fix: SystemMessage stability across restarts
    # ------------------------------------------------------------------
    system_check = _verify_system_message_stability(session_root)
    logger.info(f"D6 verification: stable={system_check['stable']}")
    logger.info(f"  reason: {system_check['reason']}")
    for s in system_check.get("system_samples", [])[:5]:
        logger.info(
            f"  {s['file']}: chars={s['system_chars']}, has_cpu={s['has_cpu_line']}, has_memory={s['has_memory_line']}"
        )

    # ------------------------------------------------------------------
    # Restart-first-call hit rate (key D6 metric)
    # ------------------------------------------------------------------
    # Only the first LLM call of each RESTART query (first query of a new
    # process) is a "restart first call". With D6 fix, bp1 (System)
    # should be byte-stable → cache hit on restart first call.
    # Within-process queries (Q2, Q3, Q5-Q7) are NOT restarts — their first
    # calls rely on bp3 (tail_anchor) carried over from the previous query
    # in the same process.
    restart_first_calls: list[dict[str, Any]] = []
    for q in per_query_usages:
        if q.get("is_restart") and q["usages"]:
            restart_first_calls.append(q["usages"][0])
    restart_first_stats = _compute_cache_hit_rate(restart_first_calls)
    logger.info(
        f"Restart-first-call hit rate: {restart_first_stats['hit_rate']}% (n={restart_first_stats['num_calls']})"
    )
    logger.info(f"  Per-call rates: {restart_first_stats['per_call_rates']}")

    # ------------------------------------------------------------------
    # Within-process hit rate (supplementary metric)
    # ------------------------------------------------------------------
    # All calls from non-restart queries (Q2-Q3 in process 0, Q5-Q7 in
    # process 1). These rely on bp3 (tail_anchor) for high hit rates.
    # The restart query's first call is excluded (it's a cold resume).
    within_process_usage = [u for q in per_query_usages if not q.get("is_restart") for u in q["usages"]]
    within_process_stats = (
        _compute_cache_hit_rate(within_process_usage) if within_process_usage else {"hit_rate": 0.0, "num_calls": 0}
    )
    logger.info(f"Within-process hit rate: {within_process_stats['hit_rate']}% (n={within_process_stats['num_calls']})")

    # ------------------------------------------------------------------
    # Persist analysis reports (no deletion of artifacts)
    # ------------------------------------------------------------------
    report_dir = session_root / "workspace" / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="v3_overall")
    _dump_cache_analysis(post_creation_usage, report_dir, label="v3_post_creation")
    _dump_cache_analysis(restart_first_calls, report_dir, label="v3_restart_first_call")
    _dump_cache_analysis(within_process_usage, report_dir, label="v3_within_process")

    dump_analysis = _analyze_context_dumps(session_root)
    dump_report_path = report_dir / "context_dump_analysis_v3.json"
    dump_report_path.write_text(json.dumps(dump_analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Context dump files analyzed: {len(dump_analysis)}")

    # Save system stability verification
    sys_report_path = report_dir / "system_stability_v3.json"
    sys_report_path.write_text(json.dumps(system_check, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"System stability report: {sys_report_path}")

    # Save per-query breakdown
    per_query_report = {
        "queries": [
            {
                "query_key": q["query_key"],
                "query": q["query"],
                "num_calls": len(q["usages"]),
                "elapsed_sec": q.get("elapsed_sec", 0),
                "process_idx": q.get("process_idx", 0),
                "is_restart": q.get("is_restart", False),
                "stats": _compute_cache_hit_rate(q["usages"]),
            }
            for q in per_query_usages
        ],
    }
    per_query_report_path = report_dir / "per_query_stats_v3.json"
    per_query_report_path.write_text(json.dumps(per_query_report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # DEFERRED assertions (placed at end, after all queries complete)
    # ------------------------------------------------------------------
    # Pre-compute a per-query summary string used in assertion messages so that
    # a failing assertion shows BOTH per-LLM-call rates AND per-question totals
    # (hit_rate, input/output/cache tokens). This makes cache regressions much
    # easier to localise to a specific query.
    per_query_summary = _format_per_query_summary(per_query_usages)

    assert len(all_usage) > 0, (
        f"No LLM call usage metadata collected across {len(query_keys)} queries\n{per_query_summary}"
    )

    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json not found at {messages_file}"

    assert overall_stats["cache_read"] > 0, (
        f"Expected some cache hits across {overall_stats['num_calls']} calls, "
        f"but got 0 cache_read tokens. Per-call: {overall_stats['per_call_rates']}\n"
        f"{per_query_summary}"
    )

    if CACHE_THRESHOLD_PROFILE == "optimized":
        assert overall_stats["hit_rate"] >= OVERALL_HIT_RATE_THRESHOLD, (
            f"Overall cache hit rate {overall_stats['hit_rate']}% < {OVERALL_HIT_RATE_THRESHOLD}%. "
            f"Per-call: {overall_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )

        assert post_creation_stats["hit_rate"] >= POST_CREATION_HIT_RATE_THRESHOLD, (
            f"Post-creation cache hit rate {post_creation_stats['hit_rate']}% < "
            f"{POST_CREATION_HIT_RATE_THRESHOLD}%. "
            f"Per-call (post-creation, {post_creation_stats['num_calls']} calls): "
            f"{post_creation_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )
    elif CACHE_THRESHOLD_PROFILE == "baseline":
        logger.info(
            f"Baseline profile: overall hit rate {overall_stats['hit_rate']}% "
            f"(threshold {OVERALL_HIT_RATE_THRESHOLD}% not enforced)"
        )
        logger.info(
            f"Baseline profile: post-creation hit rate {post_creation_stats['hit_rate']}% "
            f"(threshold {POST_CREATION_HIT_RATE_THRESHOLD}% not enforced)"
        )
    else:
        logger.info(f"Threshold profile is '{CACHE_THRESHOLD_PROFILE}': all cache hit rate assertions are disabled")

    # D6 verification: SystemMessage must NOT contain CPU/Memory lines
    assert system_check["stable"], (
        f"D6 fix verification failed: SystemMessage still contains CPU/Memory lines. "
        f"Samples: {system_check['system_samples'][:3]}"
    )

    # Restart-first-call hit rate: key metric for D6.
    # Only the first call of each RESTART query (process boundary) is
    # measured. Without D6, restart's first call had read=0 (cache rebuild).
    # With D6, bp1 should be byte-stable → first call hits the cache.
    if CACHE_THRESHOLD_PROFILE == "optimized":
        assert restart_first_stats["hit_rate"] >= RESTART_FIRST_CALL_HIT_RATE_THRESHOLD, (
            f"Restart-first-call hit rate {restart_first_stats['hit_rate']}% < "
            f"{RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%. This is the key D6 metric — "
            f"if it fails, SystemMessage is still varying across process restarts. "
            f"Restart queries: {restart_query_indices}. "
            f"Per-call (first call of each restart query, {restart_first_stats['num_calls']} calls): "
            f"{restart_first_stats['per_call_rates']}\n"
            f"{per_query_summary}"
        )
    elif CACHE_THRESHOLD_PROFILE == "baseline":
        logger.info(
            f"Baseline profile: restart-first-call hit rate {restart_first_stats['hit_rate']}% "
            f"(threshold {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}% not enforced)"
        )

    # Compression safety: bp1 (history_summary) must NOT miss after compression.
    # The only call with cache_read=0 should be Q1's first call (cold start,
    # no prior cache). If any Q2-Q7 call has cache_read=0, it means bp1 missed
    # — likely because compression broke history_summary (a regression in
    # _find_head_count or pruner logic).
    zero_read_calls = [
        {"query": q["query_key"], "call_idx": j, "input": u["input_tokens"]}
        for q in per_query_usages
        for j, u in enumerate(q["usages"])
        if u["input_cache_read_tokens"] == 0
    ]
    # Q1 (first query, cold start) is allowed one zero-read call (its first call).
    # Any additional zero-read calls indicate bp1 miss after compression.
    max_allowed_zero_reads = 1 if len(per_query_usages) > 0 else 0
    assert len(zero_read_calls) <= max_allowed_zero_reads, (
        f"Found {len(zero_read_calls)} calls with cache_read=0 (expected at most "
        f"{max_allowed_zero_reads} for Q1 cold start). This means bp1 "
        f"(history_summary) missed — compression may have broken it. "
        f"Zero-read calls: {zero_read_calls}\n"
        f"{per_query_summary}"
    )
    if zero_read_calls:
        logger.info(
            f"Compression safety: {len(zero_read_calls)} zero-read call(s) "
            f"(expected: Q1 cold start only) — bp1 survived compression ✅"
        )

    # ------------------------------------------------------------------
    # Per-query functional correctness assertions
    # ------------------------------------------------------------------
    # These checks verify that the agent produced the CORRECT answer for
    # each user question — not just that it called the LLM. They catch
    # functional regressions that pure cache-hit-rate metrics miss, e.g.
    # NL2SQL generating SQL that confuses the antibody NAME ("BD55-1111")
    # with its numeric id column (11397) and so failing to create the
    # experiment at all (the qwen-plus run on 2026-06-30 17:11 hit exactly
    # this bug: Q1 silently no-op'd, Q7 then had no experiment to report).
    #
    # Expected values are DB-verified (see EXPECTED_* constants above) and
    # cross-checked against the known-good deepseek reference run
    # cache_test_user_v3_20260630_163422_9a55, which created experiment
    # 902036 with the expected inhibitor/pseudovirus/cell sample ids.
    #
    # Assertions are SKIPPED (with a logger.warning) when their target
    # query wasn't run (e.g. --skip_slow drops Q1; --quick drops Q1-Q3
    # and Q7). This keeps the cache-smoke modes usable while still
    # enforcing correctness on the full 7-query replay.
    logger.info("=" * 60)
    logger.info("TC1 Functional correctness checks:")

    # Build query_key → response dict for fast lookup. per_query_responses
    # preserves execution order which equals QUERY_SEQUENCES order (minus
    # any skipped keys).
    response_by_key: dict[str, dict[str, Any]] = {r["query_key"]: r for r in per_query_responses}

    db_path = workspace_dir / "bio_lab.sqlite"
    today_str = datetime.now().strftime("%Y-%m-%d")
    functional_results: dict[str, dict[str, Any]] = {}

    # ---- Q1: create_experiment -----------------------------------------
    # The agent MUST have INSERTed a new neutralization_experiments row
    # with the expected inhibitor/pseudovirus/cell sample ids, status=NEW,
    # and start_date=today. The new experiment id (auto-generated, e.g.
    # 902036) is captured here and reused by the Q3 / Q7 assertions.
    created_experiment_id: int | None = None
    if "create_experiment" in response_by_key:
        created_experiment_id = _find_created_experiment_id(db_path, today_str)
        q1_answer = response_by_key["create_experiment"].get("final_answer", "")
        functional_results["create_experiment"] = {
            "created_experiment_id": created_experiment_id,
            "answer_preview": q1_answer[:300],
        }
        assert created_experiment_id is not None, (
            f"Q1 (create_experiment) FAILED: no new neutralization_experiments "
            f"row found in {db_path} with inhibitor_sample_id="
            f"{EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID}, pseudovirus_sample_id="
            f"{EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID}, cell_sample_id="
            f"{EXPECTED_HUH7_CELL_SAMPLE_ID}, status="
            f"{EXPECTED_NEW_EXPERIMENT_STATUS!r}, start_date={today_str!r}. "
            f"This means the agent did NOT actually create the experiment — "
            f"most likely NL2SQL generated a wrong lookup SQL (e.g. confused "
            f"the antibody NAME 'BD55-1111' with the numeric id column) and "
            f"the create workflow aborted before INSERT. Final answer was:\n"
            f"{q1_answer[:1000]}"
        )
        # The agent's reply should mention the new experiment id.
        assert str(created_experiment_id) in q1_answer, (
            f"Q1 (create_experiment): the agent created experiment "
            f"{created_experiment_id} in the DB but its final answer does "
            f"not mention this id. Final answer was:\n{q1_answer[:1000]}"
        )
        logger.info(
            f"  Q1 create_experiment: ✅ experiment {created_experiment_id} "
            f"created (inhibitor={EXPECTED_BD55_1111_ANTIBODY_SAMPLE_ID}, "
            f"pseudovirus={EXPECTED_XBB15_PSEUDOVIRUS_SAMPLE_ID}, "
            f"cell={EXPECTED_HUH7_CELL_SAMPLE_ID}, status="
            f"{EXPECTED_NEW_EXPERIMENT_STATUS!r}, date={today_str})"
        )
    else:
        logger.warning(
            "  Q1 create_experiment: SKIPPED (not in query sequence — "
            "--skip_slow or --quick mode). Q3/Q7 id-consistency checks "
            "will also be skipped."
        )

    # ---- Q2: find_antibody_neutralization ------------------------------
    # The agent MUST report all 5 pseudoviruses that BD-368 neutralizes
    # with IC50 < 0.1 (and fit_success=1). The response text must contain
    # every expected pseudovirus NAME (subset match is NOT enough — a
    # working agent lists all of them).
    if "find_antibody_neutralization" in response_by_key:
        q2_answer = response_by_key["find_antibody_neutralization"].get("final_answer", "")
        missing = [name for name in EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES if name not in q2_answer]
        functional_results["find_antibody_neutralization"] = {
            "missing_pseudoviruses": missing,
            "answer_preview": q2_answer[:300],
        }
        assert not missing, (
            f"Q2 (find_antibody_neutralization) FAILED: the agent's final "
            f"answer is missing {len(missing)} of the "
            f"{len(EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES)} expected "
            f"pseudovirus names (BD-368 neutralizes with IC50<0.1): "
            f"missing={missing}. Expected all of "
            f"{sorted(EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES)}. "
            f"This usually means the NL2SQL joined on the wrong column "
            f"(e.g. antibodies.id='BD-368' instead of proteins.name='BD-368') "
            f"and returned an empty / partial result. Final answer was:\n"
            f"{q2_answer[:1500]}"
        )
        logger.info(
            f"  Q2 find_antibody_neutralization: ✅ all "
            f"{len(EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES)} expected "
            f"pseudoviruses present "
            f"({sorted(EXPECTED_BD368_NEUTRALIZED_PSEUDOVIRUSES)})"
        )
    else:
        logger.info("  Q2 find_antibody_neutralization: SKIPPED (not in query sequence).")

    # ---- Q3: find_recent_experiment ------------------------------------
    # The most recent experiment for XBB.1.5 + BD55-1111 MUST be the one
    # created in Q1. If Q1 was skipped, this assertion is also skipped
    # (no reference id to compare against).
    if "find_recent_experiment" in response_by_key:
        q3_answer = response_by_key["find_recent_experiment"].get("final_answer", "")
        functional_results["find_recent_experiment"] = {
            "expected_experiment_id": created_experiment_id,
            "answer_preview": q3_answer[:300],
        }
        if created_experiment_id is None:
            logger.warning(
                "  Q3 find_recent_experiment: SKIPPED (Q1 was not run, so "
                "no reference experiment_id to assert against)."
            )
        else:
            assert str(created_experiment_id) in q3_answer, (
                f"Q3 (find_recent_experiment) FAILED: the most recent "
                f"XBB.1.5+BD55-1111 experiment is {created_experiment_id} "
                f"(created in Q1), but the agent's final answer does not "
                f"mention this id. This usually means the lookup SQL used "
                f"the wrong column (e.g. pv.name=123 instead of "
                f"pv.name='XBB.1.5') and returned no rows. "
                f"Final answer was:\n{q3_answer[:1500]}"
            )
            logger.info(f"  Q3 find_recent_experiment: ✅ reports experiment {created_experiment_id} (matches Q1)")
    else:
        logger.info("  Q3 find_recent_experiment: SKIPPED (not in query sequence).")

    # ---- Q4: count_cells -----------------------------------------------
    # Literal interpretation: total distinct cell types in the DB = 2
    # (800002 huh-7, 900300 HEK293T-ACE2). Both ids must appear in the
    # answer. The query wording explicitly says "不同类型" so the literal
    # count interpretation is unambiguous.
    if "count_cells" in response_by_key:
        q4_answer = response_by_key["count_cells"].get("final_answer", "")
        missing_ids = [cid for cid in EXPECTED_CELL_IDS if cid not in q4_answer]
        functional_results["count_cells"] = {
            "expected_count": EXPECTED_CELL_COUNT,
            "expected_ids": sorted(EXPECTED_CELL_IDS),
            "missing_ids": missing_ids,
            "answer_preview": q4_answer[:300],
        }
        assert str(EXPECTED_CELL_COUNT) in q4_answer, (
            f"Q4 (count_cells) FAILED: expected {EXPECTED_CELL_COUNT} cells "
            f"in the DB but the agent's final answer does not mention this "
            f"count. Final answer was:\n{q4_answer[:1000]}"
        )
        assert not missing_ids, (
            f"Q4 (count_cells) FAILED: the agent's final answer is missing "
            f"cell id(s) {missing_ids} (expected all of "
            f"{sorted(EXPECTED_CELL_IDS)}). Final answer was:\n{q4_answer[:1000]}"
        )
        logger.info(f"  Q4 count_cells: ✅ {EXPECTED_CELL_COUNT} cells, ids={sorted(EXPECTED_CELL_IDS)}")
    else:
        logger.info("  Q4 count_cells: SKIPPED (not in query sequence).")

    # ---- Q5: count_viruses ---------------------------------------------
    if "count_viruses" in response_by_key:
        q5_answer = response_by_key["count_viruses"].get("final_answer", "")
        missing_ids = [vid for vid in EXPECTED_PSEUDOVIRUS_IDS if vid not in q5_answer]
        functional_results["count_viruses"] = {
            "expected_count": EXPECTED_PSEUDOVIRUS_COUNT,
            "expected_ids": sorted(EXPECTED_PSEUDOVIRUS_IDS),
            "missing_ids": missing_ids,
            "answer_preview": q5_answer[:300],
        }
        assert str(EXPECTED_PSEUDOVIRUS_COUNT) in q5_answer, (
            f"Q5 (count_viruses) FAILED: expected {EXPECTED_PSEUDOVIRUS_COUNT} "
            f"pseudoviruses in the DB but the agent's final answer does not "
            f"mention this count. Final answer was:\n{q5_answer[:1000]}"
        )
        assert not missing_ids, (
            f"Q5 (count_viruses) FAILED: the agent's final answer is missing "
            f"pseudovirus id(s) {missing_ids} (expected all of "
            f"{sorted(EXPECTED_PSEUDOVIRUS_IDS)}). Final answer was:\n{q5_answer[:1000]}"
        )
        logger.info(
            f"  Q5 count_viruses: ✅ {EXPECTED_PSEUDOVIRUS_COUNT} pseudoviruses, ids={sorted(EXPECTED_PSEUDOVIRUS_IDS)}"
        )
    else:
        logger.info("  Q5 count_viruses: SKIPPED (not in query sequence).")

    # ---- Q6: count_antibodies ------------------------------------------
    if "count_antibodies" in response_by_key:
        q6_answer = response_by_key["count_antibodies"].get("final_answer", "")
        missing_ids = [aid for aid in EXPECTED_ANTIBODY_IDS if aid not in q6_answer]
        functional_results["count_antibodies"] = {
            "expected_count": EXPECTED_ANTIBODY_COUNT,
            "expected_ids": sorted(EXPECTED_ANTIBODY_IDS),
            "missing_ids": missing_ids,
            "answer_preview": q6_answer[:300],
        }
        assert str(EXPECTED_ANTIBODY_COUNT) in q6_answer, (
            f"Q6 (count_antibodies) FAILED: expected {EXPECTED_ANTIBODY_COUNT} "
            f"antibodies in the DB but the agent's final answer does not "
            f"mention this count. Final answer was:\n{q6_answer[:1000]}"
        )
        assert not missing_ids, (
            f"Q6 (count_antibodies) FAILED: the agent's final answer is "
            f"missing antibody id(s) {missing_ids} (expected all of "
            f"{sorted(EXPECTED_ANTIBODY_IDS)}). Final answer was:\n{q6_answer[:1000]}"
        )
        logger.info(
            f"  Q6 count_antibodies: ✅ {EXPECTED_ANTIBODY_COUNT} antibodies, ids={sorted(EXPECTED_ANTIBODY_IDS)}"
        )
    else:
        logger.info("  Q6 count_antibodies: SKIPPED (not in query sequence).")

    # ---- Q7: ask_recent_experiment_id ----------------------------------
    # The agent MUST report the experiment id that Q1 created. If Q1 was
    # skipped, this assertion is also skipped.
    if "ask_recent_experiment_id" in response_by_key:
        q7_answer = response_by_key["ask_recent_experiment_id"].get("final_answer", "")
        functional_results["ask_recent_experiment_id"] = {
            "expected_experiment_id": created_experiment_id,
            "answer_preview": q7_answer[:300],
        }
        if created_experiment_id is None:
            logger.warning(
                "  Q7 ask_recent_experiment_id: SKIPPED (Q1 was not run, so "
                "no reference experiment_id to assert against)."
            )
        else:
            assert str(created_experiment_id) in q7_answer, (
                f"Q7 (ask_recent_experiment_id) FAILED: the user asked for "
                f"the id of the experiment just created in Q1 — expected "
                f"{created_experiment_id}, but the agent's final answer "
                f"does not mention this id. This usually means Q1 silently "
                f"failed to create the experiment (the agent then answers "
                f"'no experiment was created'). Final answer was:\n"
                f"{q7_answer[:1500]}"
            )
            logger.info(f"  Q7 ask_recent_experiment_id: ✅ reports experiment {created_experiment_id} (matches Q1)")
    else:
        logger.info("  Q7 ask_recent_experiment_id: SKIPPED (not in query sequence).")

    # Persist the functional results alongside the cache stats so a failure
    # can be diagnosed offline from the .memory dir.
    functional_report_path = report_dir / "functional_results_v3.json"
    functional_report_path.write_text(
        json.dumps(functional_results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Functional results report: {functional_report_path}")
    logger.info("=" * 60)

    logger.info("=" * 60)
    logger.info("TC1 PASSED:")
    if CACHE_THRESHOLD_PROFILE == "optimized":
        logger.info(f"  Overall: {overall_stats['hit_rate']}% >= {OVERALL_HIT_RATE_THRESHOLD}%")
        logger.info(f"  Post-creation: {post_creation_stats['hit_rate']}% >= {POST_CREATION_HIT_RATE_THRESHOLD}%")
        logger.info(
            f"  Restart-first-call: {restart_first_stats['hit_rate']}% >= {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%"
        )
    else:
        logger.info(f"  Overall: {overall_stats['hit_rate']}% (threshold: {OVERALL_HIT_RATE_THRESHOLD}%, not enforced)")
        logger.info(
            f"  Post-creation: {post_creation_stats['hit_rate']}% (threshold: {POST_CREATION_HIT_RATE_THRESHOLD}%, not enforced)"
        )
        logger.info(
            f"  Restart-first-call: {restart_first_stats['hit_rate']}% (threshold: {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%, not enforced)"
        )
    logger.info(f"  Within-process: {within_process_stats['hit_rate']}% (n={within_process_stats['num_calls']})")
    logger.info(f"  D6 system stability: {system_check['stable']}")
    logger.info(
        f"  Functional checks: {len(functional_results)} queries verified"
        f"{f' (created_experiment_id={created_experiment_id})' if created_experiment_id else ''}"
    )
    logger.info(f"  Workspace preserved at: {workspace_dir}")
    logger.info(f"  Session root preserved at: {session_root}")
    logger.info("=" * 60)

    return {
        "overall_stats": overall_stats,
        "post_creation_stats": post_creation_stats,
        "restart_first_stats": restart_first_stats,
        "within_process_stats": within_process_stats,
        "system_check": system_check,
        "per_query_usages": per_query_usages,
        "functional_results": functional_results,
        "created_experiment_id": created_experiment_id,
        "workspace_dir": workspace_dir,
        "session_root": session_root,
    }


async def test_v3_offline_extraction(session_root: Path) -> dict[str, Any]:
    """TC2: Offline extraction from messages.json (no in-process state).

    Verifies that the messages.json file contains valid usage_metadata and
    the cache hit rate can be recomputed offline from the persisted file.

    Args:
        session_root: Path to the session root (from TC1).

    Returns:
        dict with offline extraction stats.
    """
    offline_stats = _extract_cache_from_messages_file(session_root)
    logger.info(f"TC2 Offline extraction: hit_rate={offline_stats['hit_rate']}%, calls={offline_stats['num_calls']}")

    messages_file = session_root / "workspace" / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json should exist at {messages_file}"

    assert offline_stats["num_calls"] > 0, f"Expected > 0 LLM calls in messages.json, got {offline_stats['num_calls']}"

    assert offline_stats["cache_read"] > 0, (
        f"Expected cache_read > 0 from messages.json extraction, got {offline_stats['cache_read']}"
    )

    logger.info(f"TC2 PASSED: offline extraction works, hit_rate={offline_stats['hit_rate']}%")
    return {"offline_stats": offline_stats}


def _parse_round_dump(file_path: Path) -> list[dict[str, Any]]:
    """Parse a round_X.txt dump file into a list of message dicts.

    Each message dict contains: idx, role, content (truncated), chars, bp_tag,
    cache_control markers, and whether it's an anchor.
    """
    content = file_path.read_text(encoding="utf-8")
    messages = []
    current_msg = None
    in_content = False

    for line in content.splitlines():
        header_match = re.match(r"^--- \[(\d+)\] (\w+)(.*?)---", line)
        if header_match:
            if current_msg:
                messages.append(current_msg)
            idx = int(header_match.group(1))
            role = header_match.group(2)
            header_extra = header_match.group(3).strip()

            current_msg = {
                "idx": idx,
                "role": role,
                "content_preview": "",
                "chars": 0,
                "bp_tag": "",
                "is_anchor": "[anchor]" in header_extra,
                "has_explicit_cc": "[explicit cc]" in header_extra or "[bp" in header_extra,
                "bp_candidate": "[bp" in header_extra,
                "content_lines": [],
            }
            # Extract bp candidate number
            bp_m = re.search(r"\[bp (\d) candidate\]", header_extra)
            if bp_m:
                current_msg["bp_candidate_num"] = int(bp_m.group(1))
            in_content = True
        elif in_content and current_msg is not None:
            if line.startswith("--- ["):
                messages.append(current_msg)
                current_msg = None
                in_content = False
                # Re-parse this line as a new header
                header_match = re.match(r"^--- \[(\d+)\] (\w+)(.*?)---", line)
                if header_match:
                    idx = int(header_match.group(1))
                    role = header_match.group(2)
                    header_extra = header_match.group(3).strip()
                    current_msg = {
                        "idx": idx,
                        "role": role,
                        "content_preview": "",
                        "chars": 0,
                        "bp_tag": "",
                        "is_anchor": "[anchor]" in header_extra,
                        "has_explicit_cc": "[explicit cc]" in header_extra or "[bp" in header_extra,
                        "bp_candidate": "[bp" in header_extra,
                        "content_lines": [],
                    }
                    in_content = True
            else:
                # Check for cache_control marker
                if "⭐ cache_control" in line:
                    current_msg["has_explicit_cc"] = True
                current_msg["content_lines"].append(line)

    if current_msg:
        messages.append(current_msg)

    # Calculate chars and preview
    for msg in messages:
        full_content = "\n".join(msg.get("content_lines", []))
        msg["chars"] = len(full_content)
        # Truncate content for display
        if len(full_content) > 500:
            msg["content_preview"] = full_content[:500] + "\n... (truncated)"
        else:
            msg["content_preview"] = full_content

    return messages


def generate_cache_visualization(session_root: Path) -> Path:
    """Generate a self-contained HTML visualization of cache_control markers and context evolution.

    The HTML file includes:
    1. Per-run, per-round breakpoint allocation with cache_control markers highlighted
    2. Compression detection (message count drops between rounds)
    3. Side-by-side diff of context before/after compression events

    Args:
        session_root: Path to the session root directory.

    Returns:
        Path to the generated HTML file.
    """
    import html as html_lib

    dump_dir = session_root / "workspace" / ".memory" / "context_dump"

    # Collect all runs and rounds
    runs_data: list[dict[str, Any]] = []
    if dump_dir.exists():
        for run_dir in sorted(
            dump_dir.iterdir(),
            key=lambda x: int(x.name.split("_")[1]) if x.is_dir() and x.name.startswith("run_") else 999,
        ):
            if not run_dir.is_dir():
                continue

            run_num = int(run_dir.name.split("_")[1])
            rounds_data: list[dict[str, Any]] = []

            round_files = sorted(
                run_dir.glob("round_*.txt"),
                key=lambda x: int(x.stem.split("_")[1]),
            )

            prev_msg_count = None
            for rf in round_files:
                round_num = int(rf.stem.split("_")[1])

                round_info: dict[str, Any] = {
                    "round": round_num,
                    "file": str(rf.name),
                    "messages": _parse_round_dump(rf),
                    "is_compression": False,
                    "prev_msg_count": prev_msg_count,
                }

                msg_count = len(round_info["messages"])
                if prev_msg_count is not None and msg_count < prev_msg_count:
                    round_info["is_compression"] = True

                prev_msg_count = msg_count
                rounds_data.append(round_info)

            runs_data.append({"run": run_num, "rounds": rounds_data})

    # Build HTML
    html_parts: list[str] = []
    html_parts.append("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Cache Control Visualization</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif; background: #1a1a2e; color: #e0e0e0; padding: 20px; }
h1 { color: #00d4ff; margin-bottom: 20px; font-size: 24px; }
h2 { color: #00d4ff; margin: 20px 0 10px; font-size: 18px; border-bottom: 1px solid #333; padding-bottom: 5px; }
h3 { color: #ffb74d; margin: 15px 0 8px; font-size: 15px; }
.summary-card { background: #16213e; border-radius: 8px; padding: 15px; margin-bottom: 15px; border: 1px solid #333; }
.run-tabs { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 15px; }
.run-tab { background: #16213e; border: 1px solid #333; border-radius: 6px; padding: 6px 16px; cursor: pointer; color: #aaa; font-size: 13px; transition: all 0.2s; }
.run-tab:hover { border-color: #00d4ff; color: #00d4ff; }
.run-tab.active { background: #0f3460; border-color: #00d4ff; color: #00d4ff; }
.round-list { max-height: 300px; overflow-y: auto; border: 1px solid #333; border-radius: 6px; margin-bottom: 15px; }
.round-item { padding: 6px 12px; cursor: pointer; border-bottom: 1px solid #222; font-size: 13px; transition: background 0.15s; }
.round-item:hover { background: #1a1a3e; }
.round-item.active { background: #0f3460; }
.round-item.compression { color: #ff5252; }
.round-item.compression::after { content: " ⚡压缩"; font-size: 11px; }
.message-list { max-height: 600px; overflow-y: auto; border: 1px solid #333; border-radius: 6px; }
.message-card { border-bottom: 1px solid #222; padding: 10px 12px; font-size: 13px; }
.message-card.bp { border-left: 4px solid #00e676; }
.message-card.anchor { border-left: 4px solid #ffb74d; }
.message-card.no-bp { border-left: 4px solid #555; }
.message-card.compression-removed { border-left: 4px solid #ff5252; background: rgba(255,82,82,0.05); }
.msg-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px; }
.msg-idx { color: #888; font-size: 12px; }
.msg-role { font-weight: bold; font-size: 12px; padding: 2px 8px; border-radius: 4px; }
.msg-role.SYSTEM { background: #4fc3f7; color: #000; }
.msg-role.HUMAN, .msg-role.USER { background: #81c784; color: #000; }
.msg-role.AI, .msg-role.ASSISTANT { background: #ce93d8; color: #000; }
.msg-role.TOOL { background: #ffcc80; color: #000; }
.msg-bp-tag { font-size: 11px; padding: 2px 6px; border-radius: 4px; }
.msg-bp-tag.bp { background: #00e676; color: #000; }
.msg-bp-tag.no-bp { background: #444; color: #aaa; }
.msg-bp-tag.anchor { background: #ffb74d; color: #000; }
.msg-cc-badge { background: #e91e63; color: #fff; font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-left: 5px; }
.msg-chars { color: #888; font-size: 11px; }
.msg-content { background: #0d1117; border-radius: 4px; padding: 8px; margin-top: 5px; font-family: 'Cascadia Code', 'Consolas', monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 200px; overflow-y: auto; color: #c9d1d9; }
.bp-coverage-bar { background: #222; border-radius: 4px; height: 20px; overflow: hidden; margin: 5px 0; position: relative; }
.bp-coverage-fill { height: 100%; background: linear-gradient(90deg, #00e676, #00d4ff); transition: width 0.3s; }
.bp-coverage-text { position: absolute; top: 0; left: 50%; transform: translateX(-50%); font-size: 11px; line-height: 20px; color: #fff; text-shadow: 0 0 3px #000; }
.archive-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.archive-table th { background: #0f3460; padding: 8px; text-align: left; color: #00d4ff; }
.archive-table td { padding: 6px 8px; border-bottom: 1px solid #222; }
.archive-table tr:hover { background: #1a1a3e; }
.diff-view { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 10px; }
.diff-panel { border: 1px solid #333; border-radius: 6px; overflow: hidden; }
.diff-panel-title { background: #0f3460; padding: 6px 10px; font-size: 12px; color: #00d4ff; }
.diff-panel-body { max-height: 400px; overflow-y: auto; padding: 8px; font-family: monospace; font-size: 11px; white-space: pre-wrap; word-break: break-all; }
.diff-removed { color: #ff5252; text-decoration: line-through; }
.diff-added { color: #00e676; }
.grid-2col { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
</style>
</head>
<body>
<h1>Cache Control Breakpoint Visualization</h1>
""")

    # Summary card
    total_runs = len(runs_data)
    total_rounds = sum(len(r["rounds"]) for r in runs_data)
    compression_events = sum(1 for r in runs_data for rd in r["rounds"] if rd.get("is_compression"))

    html_parts.append(f"""
<div class="summary-card">
  <h2>Overview</h2>
  <p>Runs: <b>{total_runs}</b> | Rounds: <b>{total_rounds}</b> | Compression events: <b style="color:#ff5252">{compression_events}</b></p>
</div>
""")

    # Per-run visualization
    html_parts.append("<h2>Breakpoint Allocation per Run/Round</h2>")
    html_parts.append('<div class="run-tabs">')
    for i, run in enumerate(runs_data):
        active = "active" if i == 0 else ""
        html_parts.append(f'<div class="run-tab {active}" onclick="showRun({i})">Run {run["run"]}</div>')
    html_parts.append("</div>")

    for i, run in enumerate(runs_data):
        display = "" if i == 0 else "none"
        html_parts.append(f'<div class="run-content" id="run_{i}" style="display:{display}">')

        # Round list
        html_parts.append("<h3>Rounds (click to view messages)</h3>")
        html_parts.append('<div class="round-list">')
        for j, rd in enumerate(run["rounds"]):
            active_cls = "active" if j == 0 else ""
            comp_cls = "compression" if rd.get("is_compression") else ""
            html_parts.append(
                f'<div class="round-item {active_cls} {comp_cls}" '
                f'onclick="showRound({i},{j})" id="round_tab_{i}_{j}">'
                f"Round {rd['round']} | {len(rd['messages'])} msgs"
                f"</div>"
            )
        html_parts.append("</div>")

        # Round detail containers
        for j, rd in enumerate(run["rounds"]):
            display = "" if j == 0 else "none"
            html_parts.append(f'<div class="round-detail" id="round_{i}_{j}" style="display:{display}">')

            if rd.get("is_compression"):
                html_parts.append(
                    '<div style="color:#ff5252; margin:5px 0;">⚡ Compression detected: message count dropped</div>'
                )

            # Message list
            html_parts.append('<div class="message-list">')
            for msg in rd["messages"]:
                role = msg["role"]
                idx = msg["idx"]
                chars = msg["chars"]
                is_anchor = msg.get("is_anchor", False)
                has_cc = msg.get("has_explicit_cc", False)
                bp_candidate = msg.get("bp_candidate", False)

                # Determine card class
                card_cls = "message-card "
                if bp_candidate:
                    card_cls += "bp"
                elif is_anchor:
                    card_cls += "anchor"
                else:
                    card_cls += "no-bp"

                # BP tag
                bp_tag_text = ""
                bp_tag_cls = "msg-bp-tag no-bp"
                if bp_candidate:
                    bp_num = msg.get("bp_candidate_num", "?")
                    bp_tag_text = f"bp {bp_num} candidate"
                    bp_tag_cls = "msg-bp-tag bp"
                elif is_anchor:
                    bp_tag_text = "anchor"
                    bp_tag_cls = "msg-bp-tag anchor"
                elif has_cc:
                    bp_tag_text = "explicit cc"
                    bp_tag_cls = "msg-bp-tag bp"
                else:
                    bp_tag_text = "no bp"

                # Role badge class
                role_cls = f"msg-role {role}"

                # CC badge
                cc_badge = '<span class="msg-cc-badge">cache_control</span>' if has_cc else ""

                # Content (escaped, truncated for display)
                content_preview = html_lib.escape(msg.get("content_preview", ""))

                html_parts.append(
                    f'<div class="{card_cls}">'
                    f'<div class="msg-header">'
                    f'<span><span class="msg-idx">[{idx}]</span> '
                    f'<span class="{role_cls}">{role}</span> '
                    f'<span class="{bp_tag_cls}">{bp_tag_text}</span>'
                    f"{cc_badge}</span>"
                    f'<span class="msg-chars">{chars:,} chars</span>'
                    f"</div>"
                    f'<div class="msg-content">{content_preview}</div>'
                    f"</div>"
                )
            html_parts.append("</div>")  # message-list
            html_parts.append("</div>")  # round-detail

        html_parts.append("</div>")  # run-content

    html_parts.append("""
<script>
function showRun(idx) {
    document.querySelectorAll('.run-content').forEach(el => el.style.display = 'none');
    document.querySelectorAll('.run-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('run_' + idx).style.display = '';
    event.target.classList.add('active');
}
function showRound(runIdx, roundIdx) {
    document.querySelectorAll('#run_' + runIdx + ' .round-detail').forEach(el => el.style.display = 'none');
    document.querySelectorAll('#run_' + runIdx + ' .round-item').forEach(el => el.classList.remove('active'));
    document.getElementById('round_' + runIdx + '_' + roundIdx).style.display = '';
    document.getElementById('round_tab_' + runIdx + '_' + roundIdx).classList.add('active');
}
</script>
</body>
</html>
""")

    html_content = "\n".join(html_parts)
    html_path = session_root / "workspace" / ".memory" / "cache_visualization.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding="utf-8")
    return html_path


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


def _build_test_config(workspace_dir: Path) -> Path:
    """Load config from YAML, resolve paths, override SEMANTIC_LAYER base_url, write to temp file."""
    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = _resolve_config_paths(config, workspace_dir)
    # 默认走内联 mock server（离线可复现）；仅当显式提供 SEMANTIC_SERVICE_URL
    # 时才 opt-in 到真实 semantic-service（可能是自签 https，跳过证书校验）。
    if _SEMANTIC_SERVICE_URL:
        config.setdefault("SEMANTIC_LAYER", {})["base_url"] = _SEMANTIC_SERVICE_URL
        config.setdefault("SEMANTIC_LAYER", {})["verify_ssl"] = False
    else:
        config.setdefault("SEMANTIC_LAYER", {})["base_url"] = f"http://localhost:{_MOCK_PORT}"
    config.setdefault("DATABASE", {})["db_id"] = _CHANGPING_SCENE

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
) -> None:
    """Start mock services and enter interactive terminal chat mode."""
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


async def run_single_query(query: str) -> None:
    """Run a single user query against the agent and print the response."""
    workspace_dir = Path(tempfile.mkdtemp(prefix="bio_lab_query_"))
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "bio_lab.sqlite")

    config_path = _build_cache_test_config(workspace_dir, enable_human_feedback=False)
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


async def main():
    import argparse

    global CACHE_THRESHOLD_PROFILE

    _disable_proxy_env()

    parser = argparse.ArgumentParser(description="Bio_lab cache v3.0 e2e test / interactive tool")
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
    parser.add_argument("--skip_slow", action="store_true", help="Skip the slow 'create experiment' query")
    parser.add_argument("--quick", action="store_true", help="Run only 3 fast count queries (~5 min, CI smoke test)")
    parser.add_argument("--tc2_only", action="store_true", help="Run only TC2 (offline extraction) on existing session")
    parser.add_argument(
        "--user_id",
        default=None,
        help="Override CACHE_TEST_USER_ID (mainly for --tc2_only re-extraction on a prior run). "
        "Default: timestamp-suffixed fresh ID per run.",
    )
    parser.add_argument(
        "--session_id",
        default=None,
        help="Override CACHE_TEST_SESSION_ID (mainly for --tc2_only re-extraction on a prior run). "
        "Default: timestamp-suffixed fresh ID per run.",
    )
    parser.add_argument(
        "--query",
        "-q",
        default=None,
        metavar="QUESTION",
        help="单次问答模式：直接传入问题，启动 mock 服务并获取回答后退出",
    )
    parser.add_argument(
        "--viz_only",
        action="store_true",
        help="Only generate the HTML visualization from existing context_dump (no test run).",
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_PRESETS),
        default=DEFAULT_MODEL_CHOICE,
        help=f"Model preset for MODEL.chat_model (default: {DEFAULT_MODEL_CHOICE}). "
        f"openai → openai/Qwen3.7-Plus; bailian → bailian/qwen3.7-plus (DashScope, cache_control); "
        "deepseek → deepseek-v4-flash.",
    )
    parser.add_argument(
        "--compress_message_cnt",
        type=int,
        default=DEFAULT_COMPRESS_MESSAGE_CNT,
        help=f"CONTEXT.compress_message_cnt threshold for pruner hook (default: {DEFAULT_COMPRESS_MESSAGE_CNT}).",
    )
    parser.add_argument(
        "--recent_turns",
        type=int,
        default=DEFAULT_RECENT_TURNS,
        help=f"CONTEXT.recent_turns IR-replacement threshold (default: {DEFAULT_RECENT_TURNS}). "
        "0 means replace all turns (distance >= 0 is always true).",
    )
    parser.add_argument(
        "--mock_port",
        type=int,
        default=None,
        help="MetaVisor mock server port. Default: random in [32000, 32999].",
    )
    parser.add_argument(
        "--cache-threshold-profile",
        choices=["optimized", "baseline", "off"],
        default=CACHE_THRESHOLD_PROFILE,
        help=f"Cache hit rate assertion profile (default: {CACHE_THRESHOLD_PROFILE}). "
        "optimized → enforce strict thresholds (CI gate); "
        "baseline → log but skip assertions (measurement); "
        "off → disable all hit rate assertions.",
    )
    args = parser.parse_args()

    global CACHE_TEST_USER_ID, CACHE_TEST_SESSION_ID
    global _MOCK_PORT
    if args.mock_port:
        _MOCK_PORT = args.mock_port
    elif _MOCK_PORT == 0:
        _MOCK_PORT = random.randint(32000, 32999)
    if args.user_id:
        CACHE_TEST_USER_ID = args.user_id
    if args.session_id:
        CACHE_TEST_SESSION_ID = args.session_id
    CACHE_THRESHOLD_PROFILE = args.cache_threshold_profile

    logger.info("=" * 60)
    logger.info("Bio_lab cache v3.0 e2e test starting")
    logger.info(f"  user_id   : {CACHE_TEST_USER_ID}")
    logger.info(f"  session_id : {CACHE_TEST_SESSION_ID}")
    logger.info(f"  session_root: {_resolve_session_root()}")
    logger.info(f"  model     : {args.model}")
    logger.info(f"  compress_message_cnt: {args.compress_message_cnt}")
    logger.info(f"  recent_turns: {args.recent_turns}")
    logger.info(f"  threshold_profile: {CACHE_THRESHOLD_PROFILE}")
    logger.info(f"  mock_port : {_MOCK_PORT}")
    logger.info("=" * 60)

    if args.viz_only:
        session_root = _resolve_session_root()
        html_path = generate_cache_visualization(session_root)
        logger.info(f"Visualization generated: {html_path}")
        return

    _start_mock_metavisor()
    try:
        if args.tool_mode:
            await run_tool_mode(user_id=args.user, session_id=args.session)
            return
        if args.query:
            await run_single_query(args.query)
            return

        logger.info("Starting main Agent cache v3.0 tests...")

        if args.tc2_only:
            session_root = _resolve_session_root()
            await test_v3_offline_extraction(session_root)
        else:
            tc1_result = await test_v3_session_replay(
                skip_slow=args.skip_slow,
                quick=args.quick,
                model_choice=args.model,
                compress_message_cnt=args.compress_message_cnt,
                recent_turns=args.recent_turns,
            )
            logger.info("")
            await test_v3_offline_extraction(tc1_result["session_root"])

        logger.info("All cache v3.0 tests finished.")

        session_root = _resolve_session_root()
        html_path = generate_cache_visualization(session_root)
        logger.info(f"Cache visualization: {html_path}")
    finally:
        _stop_mock_metavisor()


if __name__ == "__main__":

    def _signal_handler(sig, frame):
        _stop_mock_metavisor()
        os._exit(0)

    import signal

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    asyncio.run(main())
