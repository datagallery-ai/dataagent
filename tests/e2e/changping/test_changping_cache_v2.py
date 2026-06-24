"""E2E test for changping: main Agent cache hit rate optimization v2.0.

This test extends test_changping_cache.py with:
- Multi-turn dialogue with multiple questions (triggering context compression)
- Session history loading and resume verification
- Explicit user_id and session_id for deterministic path resolution
- DATAAGENT_CONTEXT_DUMP enabled for prompt inspection
- Cache hit rate extraction from AIMessage.usage_metadata
- Trajectory and context dump analysis
- Assertions for cache hit rate thresholds (>=50% baseline, >=35% after compression)

Test queries use short-duration questions (1-3 min each) to keep total runtime manageable.
The long-duration "create experiment" query (20 min) is excluded by default (--skip_slow).

Usage::

    export DATAAGENT_QWEN_CACHE_ANCHOR=1
    export DATAAGENT_CONTEXT_DUMP=1
    python tests/e2e/changping/test_changping_cache_v2.py
    python tests/e2e/changping/test_changping_cache_v2.py --skip_slow
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))

CHANGPING_DIR = Path(__file__).resolve().parent
CONFIG_DIR = CHANGPING_DIR / "config"

os.environ.setdefault("DATAAGENT_LOG_LEVEL", "INFO")
os.environ.setdefault("DATAAGENT_CONTEXT_DUMP", "1")
os.environ.setdefault("DATAAGENT_QWEN_CACHE_ANCHOR", "1")

from test_changping import (
    CHANGPING_DIR,
    CONFIG_DIR,
    PROJECT_DIR,
    _build_test_config,
    _cleanup_test_workspace,
    _MOCK_PORT,
    _ORIGINAL_SQLITE_PATH,
    _resolve_config_paths,
    _start_mock_metavisor,
    _stop_mock_metavisor,
    mock_ontology_env,
)

CACHE_TEST_USER_ID = "cache_test_user_v2"
CACHE_TEST_SESSION_ID = "cache_test_session_v2_001"

BASE_HIT_RATE_THRESHOLD = 80.0
POST_CREATION_HIT_RATE_THRESHOLD = 80.0
COMPRESSION_HIT_RATE_THRESHOLD = 35.0
HISTORY_LOAD_HIT_RATE_THRESHOLD = 35.0


def _build_cache_test_config(workspace_dir: Path, compress_message_cnt: int = 8, compress_token_limit: int = 200000, enable_human_feedback: bool = True) -> Path:
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

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", prefix="test_cache_v2_", delete=False) as tmp:
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
    total_cache_read = sum(r["input_cache_read_tokens"] for r in usage_records)
    total_cache_creation = sum(r["input_cache_creation_tokens"] for r in usage_records)

    if total_input == 0:
        return {"hit_rate": 0.0, "total_input": 0, "cache_read": 0, "cache_creation": 0, "num_calls": 0, "per_call_rates": []}

    per_call_rates = []
    for r in usage_records:
        rate = r["input_cache_read_tokens"] / max(r["input_tokens"], 1)
        per_call_rates.append(round(rate * 100, 1))

    return {
        "hit_rate": round(total_cache_read / total_input * 100, 1),
        "total_input": total_input,
        "cache_read": total_cache_read,
        "cache_creation": total_cache_creation,
        "num_calls": len(usage_records),
        "per_call_rates": per_call_rates,
    }


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


def _dump_cache_analysis(usage_records: list[dict[str, Any]], output_dir: Path, label: str = "") -> Path:
    analysis = {
        "label": label,
        "cache_stats": _compute_cache_hit_rate(usage_records),
        "per_call": usage_records,
    }
    out_path = output_dir / f"cache_analysis{('_' + label) if label else ''}.json"
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Cache analysis written to {out_path}")
    return out_path


SHORT_QUERIES = [
    "有多少细胞，其ID是什么",
    "有多少抗体，其ID是什么",
    "有多少病毒，其ID是什么",
]

NL2SQL_QUERY = "XBB.1.5和BD55-1111的中和实验，最近一次的实验编号是多少"

FAST_QUERIES = [
    "有多少抗体，其ID是什么",
    "有多少病毒，其ID是什么",
]


async def test_multi_turn_cache_hit_rate():
    """TC1: 多轮对话缓存命中率基础验证。

    执行 3 个短查询，验证基础缓存命中率 >= 50%。
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v2_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, compress_message_cnt=8)
    logger.info(f"TC1 config: {config_path}")

    session_root = _resolve_session_root()
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path)

    all_usage: list[dict[str, Any]] = []
    per_query_usages: list[list[dict[str, Any]]] = []
    for i, query in enumerate(SHORT_QUERIES):
        logger.info(f"[TC1 Query {i + 1}/{len(SHORT_QUERIES)}] {query}")
        initial_state = {"user_id": CACHE_TEST_USER_ID, "run_id": i}
        if i > 0:
            initial_state["max_history_messages"] = 12
        response = await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state=initial_state,
        )
        usages = _collect_usage_from_state(response)
        all_usage.extend(usages)
        per_query_usages.append(usages)
        logger.info(f"  Captured {len(usages)} Planner LLM calls")

    stats = _compute_cache_hit_rate(all_usage)
    post_creation_usage = [u for u in all_usage if u["input_cache_read_tokens"] > 0]
    post_creation_stats = _compute_cache_hit_rate(post_creation_usage) if post_creation_usage else {"hit_rate": 0.0, "num_calls": 0}

    logger.info("=" * 60)
    logger.info(f"TC1 Results:")
    logger.info(f"  Total LLM calls: {stats['num_calls']}")
    logger.info(f"  Total input tokens: {stats['total_input']}")
    logger.info(f"  Cache read tokens: {stats['cache_read']}")
    logger.info(f"  Aggregate cache hit rate: {stats['hit_rate']}%")
    logger.info(f"  Post-creation hit rate (excluding cache creation calls): {post_creation_stats['hit_rate']}% ({post_creation_stats['num_calls']} calls)")
    logger.info(f"  Per-call rates: {stats['per_call_rates']}")
    for qi, q_usages in enumerate(per_query_usages):
        q_stats = _compute_cache_hit_rate(q_usages)
        logger.info(f"  Query {qi+1}: calls={q_stats['num_calls']}, hit_rate={q_stats['hit_rate']}%")
    logger.info("=" * 60)

    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="tc1_baseline")

    dump_analysis = _analyze_context_dumps(session_root)
    logger.info(f"Context dump files found: {len(dump_analysis)}")

    dump_report_path = report_dir / "context_dump_analysis_tc1.json"
    dump_report_path.write_text(json.dumps(dump_analysis, ensure_ascii=False, indent=2), encoding="utf-8")

    context_dir = session_root / ".context"
    if context_dir.exists():
        traj_files = list(context_dir.glob("Run*_Sub0.json"))
        logger.info(f"Trajectory files: {[f.name for f in traj_files]}")

    assert len(all_usage) > 0, "No LLM call usage metadata collected"
    messages_file = session_root / ".memory" / "messages.json"
    assert messages_file.exists(), f"messages.json not found at {messages_file}"

    assert stats["cache_read"] > 0, (
        f"Expected some cache hits across {stats['num_calls']} calls, "
        f"but got 0 cache_read tokens"
    )

    assert post_creation_stats["hit_rate"] >= POST_CREATION_HIT_RATE_THRESHOLD, (
        f"Post-creation cache hit rate {post_creation_stats['hit_rate']}% < {POST_CREATION_HIT_RATE_THRESHOLD}%. "
        f"Per-call: {stats['per_call_rates']}"
    )

    logger.info(f"TC1 PASSED: overall={stats['hit_rate']}%, post_creation={post_creation_stats['hit_rate']}% >= {POST_CREATION_HIT_RATE_THRESHOLD}%")
    _cleanup_test_workspace(workspace_dir, config_path)


async def test_cache_with_compression():
    """TC2: 多轮对话 + 上下文压缩触发验证。

    执行 4 个查询（compress_message_cnt=4 强制压缩），验证压缩后缓存命中率 >= 35%。
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v2_prune_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, compress_message_cnt=4, compress_token_limit=100000)
    logger.info(f"TC2 config: {config_path}")

    session_root = _resolve_session_root()
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path)

    queries = SHORT_QUERIES + [NL2SQL_QUERY]
    all_usage: list[dict[str, Any]] = []
    all_messages_counts: list[int] = []

    for i, query in enumerate(queries):
        logger.info(f"[TC2 Query {i + 1}/{len(queries)}] {query}")
        response = await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": i},
        )
        usages = _collect_usage_from_state(response)
        all_usage.extend(usages)
        msgs = response.get("messages", []) or []
        all_messages_counts.append(len(msgs))
        logger.info(f"  Captured {len(usages)} calls, messages count: {len(msgs)}")

    stats = _compute_cache_hit_rate(all_usage)
    logger.info("=" * 60)
    logger.info(f"TC2 Results:")
    logger.info(f"  Total LLM calls: {stats['num_calls']}")
    logger.info(f"  Aggregate cache hit rate: {stats['hit_rate']}%")
    logger.info(f"  Per-call rates: {stats['per_call_rates']}")
    logger.info(f"  Messages counts per query: {all_messages_counts}")
    logger.info("=" * 60)

    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="tc2_compression")

    assert len(all_usage) > 0, "No LLM call usage metadata collected"

    assert stats["hit_rate"] >= COMPRESSION_HIT_RATE_THRESHOLD, (
        f"Expected >= {COMPRESSION_HIT_RATE_THRESHOLD}% cache hit rate with compression, "
        f"got {stats['hit_rate']}%. Per-call: {stats['per_call_rates']}"
    )

    assert stats["cache_read"] > 0, (
        f"Expected cache reads after compression, got 0"
    )

    logger.info(f"TC2 PASSED: hit_rate={stats['hit_rate']}% >= {COMPRESSION_HIT_RATE_THRESHOLD}%")
    _cleanup_test_workspace(workspace_dir, config_path)


ROUND1_QUERIES = [
    "帮我创建BD55-1111抗体和XBB.1.5病毒的中和实验（使用huh-7细胞）",
    "查一下 帮我找出抗体 'BD-368' 能有效中和（IC50小于0.1）的所有假病毒名称",
    "XBB.1.5和BD55-1111的中和实验，最近一次的实验编号是多少",
]

ROUND2_QUERIES = [
    "有多少细胞，其ID是什么",
    "有多少抗体，其ID是什么",
    "有多少病毒，其ID是什么",
]


async def test_session_resume_cache():
    """TC3: 会话恢复后缓存命中率验证。

    第一轮 session 执行 3 个长查询（创建实验、查找抗体、查询中和实验编号），
    第二轮 resume session 后执行 3 个短查询。
    不删除日志、context dump、轨迹记录，不额外设置超时限制。
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v2_resume_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, compress_message_cnt=20, enable_human_feedback=False)
    logger.info(f"TC3 config: {config_path}")

    session_root = _resolve_session_root()

    from dataagent.interface.sdk.agent import DataAgent

    phase1_usage: list[dict[str, Any]] = []
    agent = DataAgent.from_config(config_path)
    for i, query in enumerate(ROUND1_QUERIES):
        logger.info(f"[TC3 Phase1 Round1 Query {i + 1}/{len(ROUND1_QUERIES)}] {query}")
        response = await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": i},
        )
        usages = _collect_usage_from_state(response)
        phase1_usage.extend(usages)
        logger.info(f"  Captured {len(usages)} Planner LLM calls")

    phase1_stats = _compute_cache_hit_rate(phase1_usage)
    logger.info(f"TC3 Phase1 hit_rate: {phase1_stats['hit_rate']}%, calls={phase1_stats['num_calls']}")

    phase2_usage: list[dict[str, Any]] = []
    agent2 = DataAgent.from_config(config_path)
    for i, query in enumerate(ROUND2_QUERIES):
        logger.info(f"[TC3 Phase2 Round2 Query {i + 1}/{len(ROUND2_QUERIES)}] {query}")
        response2 = await agent2.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": len(ROUND1_QUERIES) + i},
        )
        usages2 = _collect_usage_from_state(response2)
        phase2_usage.extend(usages2)
        logger.info(f"  Captured {len(usages2)} Planner LLM calls")

    phase2_stats = _compute_cache_hit_rate(phase2_usage)
    combined_usage = phase1_usage + phase2_usage
    combined_stats = _compute_cache_hit_rate(combined_usage)

    logger.info("=" * 60)
    logger.info(f"TC3 Results:")
    logger.info(f"  Phase1 ({len(ROUND1_QUERIES)} queries): hit_rate={phase1_stats['hit_rate']}%, calls={phase1_stats['num_calls']}")
    logger.info(f"  Phase2 (resume + {len(ROUND2_QUERIES)} queries): hit_rate={phase2_stats['hit_rate']}%, calls={phase2_stats['num_calls']}")
    logger.info(f"  Combined hit_rate: {combined_stats['hit_rate']}%")
    logger.info(f"  Combined per-call: {combined_stats['per_call_rates']}")
    logger.info("=" * 60)

    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(phase1_usage, report_dir, label="tc3_phase1")
    _dump_cache_analysis(phase2_usage, report_dir, label="tc3_phase2_resume")
    _dump_cache_analysis(combined_usage, report_dir, label="tc3_session_resume_combined")

    dump_analysis = _analyze_context_dumps(session_root)
    dump_report_path = report_dir / "context_dump_analysis_tc3.json"
    dump_report_path.write_text(json.dumps(dump_analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"TC3 context dump files: {len(dump_analysis)}")

    context_dir = session_root / ".context"
    if context_dir.exists():
        traj_files = list(context_dir.glob("Run*_Sub0.json"))
        logger.info(f"TC3 trajectory files: {[f.name for f in traj_files]}")

    assert len(combined_usage) > 0, "No LLM call usage metadata collected"

    assert combined_stats["hit_rate"] >= HISTORY_LOAD_HIT_RATE_THRESHOLD, (
        f"Expected >= {HISTORY_LOAD_HIT_RATE_THRESHOLD}% cache hit rate after session resume, "
        f"got {combined_stats['hit_rate']}%. Per-call: {combined_stats['per_call_rates']}"
    )

    assert combined_stats["cache_read"] > 0, (
        f"Expected cache reads after session resume, got 0"
    )

    logger.info(f"TC3 PASSED: combined hit_rate={combined_stats['hit_rate']}% >= {HISTORY_LOAD_HIT_RATE_THRESHOLD}%")
    logger.info(f"TC3 workspace preserved at {workspace_dir} (no cleanup)")
    logger.info(f"TC3 session_root preserved at {session_root}")


async def test_messages_file_cache_extraction():
    """TC4: 从 messages.json 文件提取缓存命中率（离线分析）。

    执行短查询后，直接从 .memory/messages.json 读取 usage_metadata，
    验证离线分析能力。
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v2_offline_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, compress_message_cnt=8)

    session_root = _resolve_session_root()
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path)

    for i, query in enumerate(SHORT_QUERIES[:2]):
        logger.info(f"[TC4 Query {i + 1}] {query}")
        await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": i},
        )

    offline_stats = _extract_cache_from_messages_file(session_root)
    logger.info(f"TC4 Offline extraction: hit_rate={offline_stats['hit_rate']}%, calls={offline_stats['num_calls']}")

    messages_file = session_root / ".memory" / "messages.json"
    assert messages_file.exists(), "messages.json should exist after session"

    assert offline_stats["num_calls"] > 0, (
        f"Expected > 0 LLM calls in messages.json, got {offline_stats['num_calls']}"
    )

    assert offline_stats["cache_read"] > 0, (
        f"Expected cache_read > 0 from messages.json extraction, got {offline_stats['cache_read']}"
    )

    logger.info(f"TC4 PASSED: offline extraction works, hit_rate={offline_stats['hit_rate']}%")
    _cleanup_test_workspace(workspace_dir, config_path)


async def test_single_query_cache_quick():
    """TC5: 单查询快速验证 — 仅1个查询，验证缓存命中率 >= 40%。

    最快的验证方式，适合 CI 环境或快速回归测试。
    """
    workspace_dir = Path(tempfile.mkdtemp(prefix="test_cache_v2_quick_ws_"))
    workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_ORIGINAL_SQLITE_PATH, workspace_dir / "changping02.sqlite")

    config_path = _build_cache_test_config(workspace_dir, compress_message_cnt=20)
    logger.info(f"TC5 config: {config_path}")

    session_root = _resolve_session_root()
    if session_root.exists():
        shutil.rmtree(session_root, ignore_errors=True)

    from dataagent.interface.sdk.agent import DataAgent
    agent = DataAgent.from_config(config_path)

    all_usage: list[dict[str, Any]] = []
    for i, query in enumerate(FAST_QUERIES):
        logger.info(f"[TC5 Query {i + 1}/{len(FAST_QUERIES)}] {query}")
        response = await agent.chat(
            query,
            session_id=CACHE_TEST_SESSION_ID,
            initial_state={"user_id": CACHE_TEST_USER_ID, "run_id": i},
        )
        usages = _collect_usage_from_state(response)
        all_usage.extend(usages)
        logger.info(f"  Captured {len(usages)} Planner LLM calls")

    stats = _compute_cache_hit_rate(all_usage)
    post_creation_usage = [u for u in all_usage if u["input_cache_read_tokens"] > 0]
    post_creation_stats = _compute_cache_hit_rate(post_creation_usage) if post_creation_usage else {"hit_rate": 0.0, "num_calls": 0}

    logger.info("=" * 60)
    logger.info(f"TC5 Results:")
    logger.info(f"  Total LLM calls: {stats['num_calls']}")
    logger.info(f"  Aggregate cache hit rate: {stats['hit_rate']}%")
    logger.info(f"  Post-creation hit rate: {post_creation_stats['hit_rate']}% ({post_creation_stats['num_calls']} calls)")
    logger.info(f"  Per-call rates: {stats['per_call_rates']}")
    logger.info("=" * 60)

    report_dir = session_root / ".memory"
    report_dir.mkdir(parents=True, exist_ok=True)
    _dump_cache_analysis(all_usage, report_dir, label="tc5_quick")

    assert len(all_usage) > 0, "No LLM call usage metadata collected"
    assert stats["cache_read"] > 0, (
        f"Expected cache hits, got 0 cache_read tokens"
    )

    assert post_creation_stats["hit_rate"] >= POST_CREATION_HIT_RATE_THRESHOLD, (
        f"Post-creation cache hit rate {post_creation_stats['hit_rate']}% < {POST_CREATION_HIT_RATE_THRESHOLD}%"
    )

    logger.info(f"TC5 PASSED: overall={stats['hit_rate']}%, post_creation={post_creation_stats['hit_rate']}% >= {POST_CREATION_HIT_RATE_THRESHOLD}%")
    _cleanup_test_workspace(workspace_dir, config_path)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Changping cache v2.0 e2e test")
    parser.add_argument("--skip_slow", action="store_true", help="Skip TC2/TC3 (slow multi-query tests)")
    parser.add_argument("--tc1_only", action="store_true", help="Run only TC1 (baseline)")
    parser.add_argument("--tc3_only", action="store_true", help="Run only TC3 (session resume cache)")
    parser.add_argument("--quick", action="store_true", help="Run only TC5 (single query quick test)")
    args = parser.parse_args()

    _start_mock_metavisor()
    try:
        with mock_ontology_env():
            logger.info("Starting main Agent cache v2.0 tests...")

            if args.quick:
                await test_single_query_cache_quick()
            elif args.tc3_only:
                await test_session_resume_cache()
            else:
                await test_multi_turn_cache_hit_rate()
                logger.info("")

                if not args.skip_slow and not args.tc1_only:
                    await test_cache_with_compression()
                    logger.info("")

                    await test_session_resume_cache()
                    logger.info("")

                await test_messages_file_cache_extraction()
                logger.info("")

            logger.info("All cache v2.0 tests finished.")
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
