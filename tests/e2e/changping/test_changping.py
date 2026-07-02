"""E2E test for changping: main Agent cache hit rate optimization v3.0.

This test verifies the v3.0 cache optimization (D6: move runtime_environment
out of SystemMessage into VariableUser) by replaying the user's real
2026-06-22 session query sequence. Each query runs in a fresh Agent process
(simulating the original test harness behavior that exposed the bug).

Key fixes verified by this test:
- D6: SystemMessage no longer contains dynamic CPU%/Memory% values, so bp 1
  (System) cache prefix stays byte-stable across process restarts.
- D8: max_history_messages is set to avoid loading the full history each restart.
- D2.1: DATAAGENT_QWEN_CACHE_BREAKPOINT_ANNOTATION=1 enables bp position
  annotation in context_dump for offline inspection.

Design principles (per user request):
- No time-based assertions (queries can take arbitrarily long).
- Do NOT delete intermediate logs, context dumps, trajectories, or workspace
  artifacts — they are preserved for offline analysis.
- Cache hit rate assertions are placed at the END of the test, after all
  queries have completed, so the test runs to completion before any failure.

Usage::

    export DATAAGENT_QWEN_CACHE_ANCHOR=1
    export DATAAGENT_CONTEXT_DUMP=1
    export DATAAGENT_QWEN_CACHE_BREAKPOINT_ANNOTATION=1
    python tests/e2e/changping/test_changping_cache_v3.py
    python tests/e2e/changping/test_changping_cache_v3.py --skip_slow
    python tests/e2e/changping/test_changping_cache_v3.py --tc2_only \\
        --user_id cache_test_user_v3_20260622_141023_ab12 \\
        --session_id cache_test_session_v3_20260622_141023_ab12

Each run auto-generates a fresh timestamped user_id/session_id under
dataagent_home() (e.g. ``cache_test_user_v3_20260623_141023_ab12``), so
historical artifacts never collide and never need manual archiving.
"""

import asyncio
import json
import os
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

CHANGPING_DIR = Path(__file__).resolve().parent
CONFIG_DIR = CHANGPING_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_QWEN_CACHE_ANCHOR", "1")
os.environ.setdefault("DATAAGENT_QWEN_CACHE_BREAKPOINT_ANNOTATION", "1")

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
    _mock_server = HTTPServer(("127.0.0.1", _MOCK_PORT), _MockMVHandler)
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

OVERALL_HIT_RATE_THRESHOLD = 50.0
POST_CREATION_HIT_RATE_THRESHOLD = 80.0
RESTART_FIRST_CALL_HIT_RATE_THRESHOLD = 50.0


def _build_cache_test_config(
    workspace_dir: Path,
    compress_message_cnt: int = 20,
    compress_token_limit: int = 200000,
    enable_human_feedback: bool = True,
) -> Path:
    import yaml

    config_path = CONFIG_DIR / "main_config.yaml"
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    config = _resolve_config_paths(config, workspace_dir)
    config.setdefault("METAVISOR", {})["metavisor_url"] = f"http://localhost:{_MOCK_PORT}"

    context_cfg = config.setdefault("CONTEXT", {})
    context_cfg["compress_message_cnt"] = compress_message_cnt
    context_cfg["compress_token_limit"] = compress_token_limit

    config.setdefault("AGENT_CONFIG", {})["enable_human_feedback"] = enable_human_feedback

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_cache_v3_", delete=False) as tmp:
        yaml.safe_dump(config, tmp, allow_unicode=True, sort_keys=False)
        tmp.flush()
        return Path(tmp.name)


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
                    usage_list.append({
                        "input_tokens": usage.get("input_tokens", 0),
                        "input_cache_read_tokens": usage.get("input_cache_read_tokens", 0),
                        "input_cache_creation_tokens": usage.get("input_cache_creation_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    })
    return usage_list


def _collect_usage_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    messages = state.get("messages", []) or []
    return _collect_usage_metadata_from_messages(messages)


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
    messages_file = session_root / ".memory" / "messages.json"
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
    dump_base = session_root / ".memory" / "context_dump"
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
            analysis.append({
                "file": str(round_file.relative_to(dump_base)),
                "message_count": msg_count,
                "file_size": len(content),
            })
    return analysis


def _verify_system_message_stability(session_root: Path) -> dict[str, Any]:
    """Verify D6 fix: SystemMessage should NOT contain CPU:/Memory: lines.

    Across all context dumps, the SYSTEM section should be byte-identical
    (no dynamic runtime_environment values).
    """
    import re

    dump_base = session_root / ".memory" / "context_dump"
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
        system_samples.append({
            "file": str(round_file.relative_to(dump_base)),
            "system_chars": len(system_text),
            "has_cpu_line": has_cpu,
            "has_memory_line": has_memory,
            "system_hash": hash(system_text),
        })
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
        "query": "有多少个细胞，ID是什么",
        "needs_feedback": False,
    },
    "count_viruses": {
        "query": "有多少个病毒，ID是什么",
        "needs_feedback": False,
    },
    "count_antibodies": {
        "query": "一共有多少个抗体，ID是多少",
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


async def test_v3_session_replay(
    skip_slow: bool = False,
    quick: bool = False,
) -> dict[str, Any]:
    """TC1: Replay the user's real 2026-06-22 session query sequence.

    Each query runs in a fresh DataAgent instance (simulating per-query process
    restart as in the original failing test), with max_history_messages=12 to
    trigger history folding on resume.

    Artifacts (logs, context dumps, trajectories) are preserved — no cleanup.
    Cache hit rate assertions are deferred to the end of the test.

    Args:
        skip_slow: If True, skip the "create experiment" query (takes ~10 min).
        quick: If True, run only 3 fast count queries (~5 min total, CI smoke test).

    Returns:
        dict with usage stats, per-query stats, and verification results.
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v3_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(
        workspace_dir,
        compress_message_cnt=20,
        enable_human_feedback=True,
    )
    logger.info(f"TC1 config: {config_path}")
    logger.info(f"TC1 workspace: {workspace_dir} (preserved after test)")

    session_root = _resolve_session_root()
    # Each run uses a timestamp-suffixed user_id/session_id, so this directory
    # is fresh by construction. The rmtree below is a defensive guard for the
    # rare case of same-second collisions or an explicit --user_id/--session_id
    # override pointing at a pre-existing dir. Artifacts from THIS run are
    # preserved after the test completes (no automatic cleanup).
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)
        logger.info(f"TC1 cleaned up pre-existing session_root: {session_root}")
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

    for i, query_key in enumerate(query_keys):
        spec = QUERY_SEQUENCES[query_key]
        query = spec["query"]
        logger.info("=" * 60)
        logger.info(f"[TC1 Query {i + 1}/{len(query_keys)}] key={query_key}")
        logger.info(f"  query={query!r}")
        logger.info(f"  needs_feedback={spec.get('needs_feedback', False)}")

        # Fresh DataAgent per query (simulates process restart in original test)
        from dataagent.interface.sdk.agent import DataAgent
        agent = DataAgent.from_config(config_path)

        initial_state = {
            "user_id": CACHE_TEST_USER_ID,
            "run_id": i,
            "max_history_messages": 12,  # D8: enable history folding on resume
        }

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

        usages = _collect_usage_from_state(response)
        all_usage.extend(usages)
        per_query_usages.append({
            "query_key": query_key,
            "query": query,
            "usages": usages,
            "elapsed_sec": elapsed_sec,
        })

        # Capture summary without storing the full state (could be huge)
        msgs = response.get("messages", []) or []
        per_query_responses.append({
            "query_key": query_key,
            "num_messages": len(msgs),
            "num_llm_calls": len(usages),
            "elapsed_sec": elapsed_sec,
        })
        logger.info(
            f"  Captured {len(usages)} Planner LLM calls, {len(msgs)} messages "
            f"in {elapsed_sec}s"
        )

    # ------------------------------------------------------------------
    # Aggregate statistics
    # ------------------------------------------------------------------
    overall_stats = _compute_cache_hit_rate(all_usage)
    post_creation_usage = [u for u in all_usage if u["input_cache_read_tokens"] > 0]
    post_creation_stats = (
        _compute_cache_hit_rate(post_creation_usage)
        if post_creation_usage
        else {"hit_rate": 0.0, "num_calls": 0}
    )

    logger.info("=" * 60)
    logger.info("TC1 Results:")
    logger.info(f"  Total LLM calls: {overall_stats['num_calls']}")
    logger.info(f"  Total input tokens: {overall_stats['total_input']}")
    logger.info(f"  Total output tokens: {overall_stats['total_output']}")
    logger.info(f"  Cache read tokens: {overall_stats['cache_read']}")
    logger.info(f"  Cache creation tokens: {overall_stats['cache_creation']}")
    logger.info(f"  Overall hit rate: {overall_stats['hit_rate']}%")
    logger.info(f"  Post-creation hit rate: {post_creation_stats['hit_rate']}% ({post_creation_stats['num_calls']} calls)")
    logger.info(f"  Per-call rates: {overall_stats['per_call_rates']}")
    logger.info("  Per-query breakdown:")
    for i, q in enumerate(per_query_usages, 1):
        q_stats = _compute_cache_hit_rate(q["usages"])
        logger.info(
            f"  [{i}/{len(per_query_usages)}] Query '{q['query_key']}': "
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
            f"  {s['file']}: chars={s['system_chars']}, "
            f"has_cpu={s['has_cpu_line']}, has_memory={s['has_memory_line']}"
        )

    # ------------------------------------------------------------------
    # Restart-first-call hit rate (key D6 metric)
    # ------------------------------------------------------------------
    # Each query's first LLM call is the "restart first call". With D6 fix,
    # bp 1 (System) should be byte-stable → cache hit on first call too.
    restart_first_calls: list[dict[str, Any]] = []
    for q in per_query_usages:
        if q["usages"]:
            restart_first_calls.append(q["usages"][0])
    restart_first_stats = _compute_cache_hit_rate(restart_first_calls)
    logger.info(f"Restart-first-call hit rate: {restart_first_stats['hit_rate']}% (n={restart_first_stats['num_calls']})")
    logger.info(f"  Per-call rates: {restart_first_stats['per_call_rates']}")

    # ------------------------------------------------------------------
    # Persist analysis reports (no deletion of artifacts)
    # ------------------------------------------------------------------
    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="v3_overall")
    _dump_cache_analysis(post_creation_usage, report_dir, label="v3_post_creation")
    _dump_cache_analysis(restart_first_calls, report_dir, label="v3_restart_first_call")

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
        f"No LLM call usage metadata collected across {len(query_keys)} queries\n"
        f"{per_query_summary}"
    )

    messages_file = session_root / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json not found at {messages_file}"

    assert overall_stats["cache_read"] > 0, (
        f"Expected some cache hits across {overall_stats['num_calls']} calls, "
        f"but got 0 cache_read tokens. Per-call: {overall_stats['per_call_rates']}\n"
        f"{per_query_summary}"
    )

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

    # D6 verification: SystemMessage must NOT contain CPU/Memory lines
    assert system_check["stable"], (
        f"D6 fix verification failed: SystemMessage still contains CPU/Memory lines. "
        f"Samples: {system_check['system_samples'][:3]}"
    )

    # Restart-first-call hit rate: key metric for D6.
    # Without D6, every restart's first call had read=0 (cache rebuild).
    # With D6, bp 1 should be byte-stable → first call hits the cache.
    assert restart_first_stats["hit_rate"] >= RESTART_FIRST_CALL_HIT_RATE_THRESHOLD, (
        f"Restart-first-call hit rate {restart_first_stats['hit_rate']}% < "
        f"{RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%. This is the key D6 metric — "
        f"if it fails, SystemMessage is still varying across process restarts. "
        f"Per-call (first call of each query, {restart_first_stats['num_calls']} calls): "
        f"{restart_first_stats['per_call_rates']}\n"
        f"{per_query_summary}"
    )

    logger.info("=" * 60)
    logger.info("TC1 PASSED:")
    logger.info(f"  Overall: {overall_stats['hit_rate']}% >= {OVERALL_HIT_RATE_THRESHOLD}%")
    logger.info(f"  Post-creation: {post_creation_stats['hit_rate']}% >= {POST_CREATION_HIT_RATE_THRESHOLD}%")
    logger.info(f"  Restart-first-call: {restart_first_stats['hit_rate']}% >= {RESTART_FIRST_CALL_HIT_RATE_THRESHOLD}%")
    logger.info(f"  D6 system stability: {system_check['stable']}")
    logger.info(f"  Workspace preserved at: {workspace_dir}")
    logger.info(f"  Session root preserved at: {session_root}")
    logger.info("=" * 60)

    return {
        "overall_stats": overall_stats,
        "post_creation_stats": post_creation_stats,
        "restart_first_stats": restart_first_stats,
        "system_check": system_check,
        "per_query_usages": per_query_usages,
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

    messages_file = session_root / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json should exist at {messages_file}"

    assert offline_stats["num_calls"] > 0, (
        f"Expected > 0 LLM calls in messages.json, got {offline_stats['num_calls']}"
    )

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


def _parse_breakpoint_summary(file_path: Path) -> dict[str, Any]:
    """Parse a round_X_breakpoints.txt file into bp allocation data."""
    content = file_path.read_text(encoding="utf-8")

    result: dict[str, Any] = {
        "messages": [],
        "bp_summary": [],
        "total_breakpoints": 0,
        "max_breakpoints": 4,
        "stable_prefix_chars": 0,
        "total_chars": 0,
        "coverage_pct": 0,
    }

    bp_pattern = re.compile(
        r"  \[(\d+)\] (\w+)(.*?)→ (bp \d+|no bp)(.*?)(?:$|\n)"
    )

    for line in content.splitlines():
        m = bp_pattern.match(line)
        if m:
            idx = int(m.group(1))
            role = m.group(2)
            extra = m.group(3)
            bp_tag = m.group(4)
            details = m.group(5)

            chars_match = re.search(r"\((\d+) chars", details)
            chars = int(chars_match.group(1)) if chars_match else 0

            cum_match = re.search(r"(\d+) cumulative", details)
            cumulative = int(cum_match.group(1)) if cum_match else 0

            spacing_match = re.search(r"spacing (\d+)", details)
            spacing = int(spacing_match.group(1)) if spacing_match else None

            is_anchor = "anchor" in extra
            cc_type = ""
            if "explicit cc" in details:
                cc_type = "explicit"
            elif "auto cc" in details:
                cc_type = "auto"

            result["messages"].append({
                "idx": idx,
                "role": role,
                "bp_tag": bp_tag,
                "chars": chars,
                "cumulative": cumulative,
                "spacing": spacing,
                "is_anchor": is_anchor,
                "cc_type": cc_type,
            })

    # Parse bp summary section
    for line in content.splitlines():
        m = re.match(r"  (bp \d+): \[(\d+)\] (\w+).*?\((\d+) chars.*?\) — (\w+)", line)
        if m:
            result["bp_summary"].append({
                "bp": m.group(1),
                "idx": int(m.group(2)),
                "role": m.group(3),
                "chars": int(m.group(4)),
                "type": m.group(5),
            })

    # Parse totals
    tb_match = re.search(r"Total breakpoints used: (\d+)/(\d+)", content)
    if tb_match:
        result["total_breakpoints"] = int(tb_match.group(1))
        result["max_breakpoints"] = int(tb_match.group(2))

    sp_match = re.search(r"Stable prefix coverage.*: (\d+) chars", content)
    if sp_match:
        result["stable_prefix_chars"] = int(sp_match.group(1))

    tc_match = re.search(r"Total message chars: (\d+)", content)
    if tc_match:
        result["total_chars"] = int(tc_match.group(1))

    cr_match = re.search(r"Prefix coverage ratio: ([\d.]+)%", content)
    if cr_match:
        result["coverage_pct"] = float(cr_match.group(1))

    return result


def _parse_archived_messages(mem_dir: Path) -> list[dict[str, Any]]:
    """Parse all archived messages.*.json files to build per-save cache progression."""
    archives = []
    for f in sorted(mem_dir.glob("messages.*.json")):
        try:
            data = json.loads(f.read_text())
            msgs = data.get("messages", [])
            summaries = data.get("round_summaries", [])

            total_in = sum(s.get("input_tokens", 0) for s in summaries)
            total_read = sum(s.get("input_cache_read_tokens", 0) for s in summaries)
            total_create = sum(s.get("input_cache_creation_tokens", 0) for s in summaries)
            total_out = sum(s.get("output_tokens", 0) for s in summaries)

            hit_rate = total_read / total_in * 100 if total_in > 0 else 0

            # Extract timestamp from filename
            ts_match = re.search(r"messages\.(\d{8}_\d{6})\.json", f.name)
            timestamp = ts_match.group(1) if ts_match else f.name

            archives.append({
                "file": f.name,
                "timestamp": timestamp,
                "msg_count": len(msgs),
                "round_count": len(summaries),
                "input_tokens": total_in,
                "cache_read": total_read,
                "cache_creation": total_create,
                "output_tokens": total_out,
                "hit_rate": round(hit_rate, 1),
            })
        except Exception:
            pass

    # Also include the final messages.json
    final = mem_dir / "messages.json"
    if final.exists():
        try:
            data = json.loads(final.read_text())
            msgs = data.get("messages", [])
            summaries = data.get("round_summaries", [])
            total_in = sum(s.get("input_tokens", 0) for s in summaries)
            total_read = sum(s.get("input_cache_read_tokens", 0) for s in summaries)
            total_create = sum(s.get("input_cache_creation_tokens", 0) for s in summaries)
            total_out = sum(s.get("output_tokens", 0) for s in summaries)
            hit_rate = total_read / total_in * 100 if total_in > 0 else 0

            archives.append({
                "file": "messages.json (final)",
                "timestamp": "final",
                "msg_count": len(msgs),
                "round_count": len(summaries),
                "input_tokens": total_in,
                "cache_read": total_read,
                "cache_creation": total_create,
                "output_tokens": total_out,
                "hit_rate": round(hit_rate, 1),
            })
        except Exception:
            pass

    return archives


def generate_cache_visualization(session_root: Path) -> Path:
    """Generate a self-contained HTML visualization of cache_control markers and context evolution.

    The HTML file includes:
    1. Per-run, per-round breakpoint allocation with cache_control markers highlighted
    2. Compression detection (message count drops between rounds)
    3. Cache hit rate progression across archived messages.json files
    4. Side-by-side diff of context before/after compression events

    Args:
        session_root: Path to the session root directory.

    Returns:
        Path to the generated HTML file.
    """
    import html as html_lib

    dump_dir = session_root / ".memory" / "context_dump"
    mem_dir = session_root / ".memory"

    # Collect all runs and rounds
    runs_data: list[dict[str, Any]] = []
    if dump_dir.exists():
        for run_dir in sorted(dump_dir.iterdir(), key=lambda x: int(x.name.split("_")[1]) if x.is_dir() and x.name.startswith("run_") else 999):
            if not run_dir.is_dir():
                continue

            run_num = int(run_dir.name.split("_")[1])
            rounds_data: list[dict[str, Any]] = []

            round_files = sorted(
                [f for f in run_dir.glob("round_*.txt") if not f.name.endswith("_breakpoints.txt")],
                key=lambda x: int(x.stem.split("_")[1]),
            )

            prev_msg_count = None
            for rf in round_files:
                round_num = int(rf.stem.split("_")[1])
                bp_file = run_dir / f"round_{round_num}_breakpoints.txt"

                round_info: dict[str, Any] = {
                    "round": round_num,
                    "file": str(rf.name),
                    "messages": _parse_round_dump(rf),
                    "bp_summary": None,
                    "is_compression": False,
                    "prev_msg_count": prev_msg_count,
                }

                if bp_file.exists():
                    round_info["bp_summary"] = _parse_breakpoint_summary(bp_file)

                msg_count = len(round_info["messages"])
                if prev_msg_count is not None and msg_count < prev_msg_count:
                    round_info["is_compression"] = True

                prev_msg_count = msg_count
                rounds_data.append(round_info)

            runs_data.append({"run": run_num, "rounds": rounds_data})

    # Collect archived messages progression
    archives = _parse_archived_messages(mem_dir)

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
.hit-rate-bar { display: inline-block; width: 60px; height: 14px; background: #222; border-radius: 3px; overflow: hidden; vertical-align: middle; }
.hit-rate-fill { height: 100%; background: #00e676; }
.hit-rate-fill.low { background: #ff5252; }
.hit-rate-fill.mid { background: #ffb74d; }
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
  <p>Runs: <b>{total_runs}</b> | Rounds: <b>{total_rounds}</b> | Compression events: <b style="color:#ff5252">{compression_events}</b> | Archived files: <b>{len(archives)}</b></p>
</div>
""")

    # Archive progression table
    if archives:
        html_parts.append("<h2>Cache Hit Rate Progression (per save_messages call)</h2>")
        html_parts.append('<div style="overflow-x:auto;"><table class="archive-table">')
        html_parts.append("<tr><th>File</th><th>Msgs</th><th>Rounds</th><th>Input Tokens</th><th>Cache Read</th><th>Cache Create</th><th>Hit Rate</th></tr>")
        for a in archives:
            hr_class = "low" if a["hit_rate"] < 50 else ("mid" if a["hit_rate"] < 70 else "")
            html_parts.append(
                f'<tr><td>{html_lib.escape(a["file"])}</td>'
                f'<td>{a["msg_count"]}</td><td>{a["round_count"]}</td>'
                f'<td>{a["input_tokens"]:,}</td>'
                f'<td>{a["cache_read"]:,}</td><td>{a["cache_creation"]:,}</td>'
                f'<td>{a["hit_rate"]}% '
                f'<span class="hit-rate-bar"><span class="hit-rate-fill {hr_class}" style="width:{min(a["hit_rate"],100)}%"></span></span>'
                f'</td></tr>'
            )
        html_parts.append("</table></div>")

    # Per-run visualization
    html_parts.append("<h2>Breakpoint Allocation per Run/Round</h2>")
    html_parts.append('<div class="run-tabs">')
    for i, run in enumerate(runs_data):
        active = "active" if i == 0 else ""
        html_parts.append(f'<div class="run-tab {active}" onclick="showRun({i})">Run {run["run"]}</div>')
    html_parts.append('</div>')

    for i, run in enumerate(runs_data):
        display = "" if i == 0 else "none"
        html_parts.append(f'<div class="run-content" id="run_{i}" style="display:{display}">')

        # Round list
        html_parts.append('<h3>Rounds (click to view messages)</h3>')
        html_parts.append('<div class="round-list">')
        for j, rd in enumerate(run["rounds"]):
            active_cls = "active" if j == 0 else ""
            comp_cls = "compression" if rd.get("is_compression") else ""
            bp = rd.get("bp_summary") or {}
            bp_count = bp.get("total_breakpoints", 0)
            coverage = bp.get("coverage_pct", 0)
            html_parts.append(
                f'<div class="round-item {active_cls} {comp_cls}" '
                f'onclick="showRound({i},{j})" id="round_tab_{i}_{j}">'
                f'Round {rd["round"]} | {len(rd["messages"])} msgs | bp={bp_count}/4 | cov={coverage:.0f}%'
                f'</div>'
            )
        html_parts.append('</div>')

        # Round detail containers
        for j, rd in enumerate(run["rounds"]):
            display = "" if j == 0 else "none"
            html_parts.append(f'<div class="round-detail" id="round_{i}_{j}" style="display:{display}">')

            bp = rd.get("bp_summary") or {}
            if bp:
                coverage = bp.get("coverage_pct", 0)
                stable = bp.get("stable_prefix_chars", 0)
                total = bp.get("total_chars", 0)
                html_parts.append(
                    f'<div class="bp-coverage-bar">'
                    f'<div class="bp-coverage-fill" style="width:{coverage:.0f}%"></div>'
                    f'<span class="bp-coverage-text">Coverage: {stable:,}/{total:,} chars ({coverage:.1f}%) | bp: {bp.get("total_breakpoints",0)}/{bp.get("max_breakpoints",4)}</span>'
                    f'</div>'
                )

                # BP summary table
                if bp.get("bp_summary"):
                    html_parts.append('<table class="archive-table" style="margin:5px 0;">')
                    html_parts.append("<tr><th>BP</th><th>Idx</th><th>Role</th><th>Chars</th><th>Type</th></tr>")
                    for bps in bp["bp_summary"]:
                        html_parts.append(
                            f'<tr><td>{html_lib.escape(bps["bp"])}</td><td>{bps["idx"]}</td>'
                            f'<td>{bps["role"]}</td><td>{bps["chars"]:,}</td>'
                            f'<td>{html_lib.escape(bps["type"])}</td></tr>'
                        )
                    html_parts.append("</table>")

            if rd.get("is_compression"):
                html_parts.append('<div style="color:#ff5252; margin:5px 0;">⚡ Compression detected: message count dropped</div>')

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
                    f'{cc_badge}</span>'
                    f'<span class="msg-chars">{chars:,} chars</span>'
                    f'</div>'
                    f'<div class="msg-content">{content_preview}</div>'
                    f'</div>'
                )
            html_parts.append('</div>')  # message-list
            html_parts.append('</div>')  # round-detail

        html_parts.append('</div>')  # run-content

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
    html_path = session_root / ".memory" / "cache_visualization.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html_content, encoding="utf-8")
    return html_path


# ---------------------------------------------------------------------------
# Tool mode — interactive CLI with mock services
# ---------------------------------------------------------------------------
def _create_test_workspace() -> Path:
    """Create a temporary workspace directory and copy the sqlite DB into it."""
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_changping_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")
    logger.info(f"Test workspace created: {workspace_dir} (sqlite DB copied)")
    return workspace_dir


def _build_test_config(workspace_dir: Path) -> Path:
    """Load config from YAML, resolve paths, override METAVISOR URL, write to temp file."""
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
    workspace_dir = Path(tempfile.mkdtemp(prefix="changping_query_"))
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, enable_human_feedback=False)
    logger.info(f"Query mode config: {config_path}")

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(str(config_path))
    try:
        response = await agent.chat(query, session_id=None)
        final = response if isinstance(response, str) else getattr(response, "content", str(response))
        print(f"\n{'='*60}")
        print(f"问题: {query}")
        print(f"{'='*60}")
        print(f"回答: {final}")
        print(f"{'='*60}\n")
    finally:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        config_path.unlink(missing_ok=True)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Changping cache v3.0 e2e test / interactive tool")
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
    args = parser.parse_args()

    global CACHE_TEST_USER_ID, CACHE_TEST_SESSION_ID
    if args.user_id:
        CACHE_TEST_USER_ID = args.user_id
    if args.session_id:
        CACHE_TEST_SESSION_ID = args.session_id

    logger.info("=" * 60)
    logger.info("Changping cache v3.0 e2e test starting")
    logger.info(f"  user_id   : {CACHE_TEST_USER_ID}")
    logger.info(f"  session_id : {CACHE_TEST_SESSION_ID}")
    logger.info(f"  session_root: {_resolve_session_root()}")
    logger.info("=" * 60)

    if args.viz_only:
        session_root = _resolve_session_root()
        html_path = generate_cache_visualization(session_root)
        logger.info(f"Visualization generated: {html_path}")
        return

    _start_mock_metavisor()
    try:
        with mock_ontology_env():
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
                tc1_result = await test_v3_session_replay(skip_slow=args.skip_slow, quick=args.quick)
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
