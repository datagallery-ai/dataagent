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
"""
单条 query 的 NL2SQL 评测脚本：指定 query、SQLite 路径、输出目录，跑一次并写出
1) 生成的 SQL 文件 ({db_id}.sql)
2) Agent 轨迹 (trajectory.json)
3) 单次运行结果汇总 (agent_result.json)
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.agents.nl2sql.workflow.state import get_default_state
from dataagent.interface.sdk.agent import DataAgent

PROJECT_DIR = Path(__file__).resolve().parents[2]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


def _sanitize_state(state: dict[str, Any]) -> dict[str, Any]:
    """将 state 转为可 JSON 序列化的结构。"""
    if not isinstance(state, dict):
        return {"_raw": str(state)}
    out: dict[str, Any] = {}
    for k, v in state.items():
        if k in ("messages",) and isinstance(v, list) and len(v) > 20:
            out[k] = f"<list len={len(v)}>"
        elif hasattr(v, "__dict__") and not isinstance(v, (dict, list, str, int, float, bool, type(None))):
            out[k] = str(v)
        elif isinstance(v, dict):
            out[k] = _sanitize_state(v)
        elif isinstance(v, list):
            out[k] = [
                _sanitize_state(x)
                if isinstance(x, dict)
                else (str(x) if not isinstance(x, (str, int, float, bool, type(None))) else x)
                for x in v[:50]
            ]
            if len(v) > 50:
                out[k].append(f"<truncated {len(v) - 50} more>")
        else:
            try:
                json.dumps(v)
                out[k] = v
            except (TypeError, ValueError):
                out[k] = str(v)
    return out


def _state_to_response(node_name: str, state: dict[str, Any]) -> str | dict[str, Any]:
    """根据 node 名从 state 中提取本步大模型/动作的主要回复内容。"""
    if not isinstance(state, dict):
        return str(state)
    if node_name == "coordinator":
        q = state.get("semantic_query") or ""
        kw = state.get("keywords") or []
        return {"semantic_query": q[:500], "keywords": kw[:20]}
    if node_name == "perceptor":
        reasoning = (state.get("reasoning") or "").strip()
        if reasoning:
            return reasoning[:1000]
        schema = state.get("schema") or {}
        return {"schema_tables": list(schema.keys())[:30]}
    if node_name == "generator":
        return state.get("sql") or state.get("prompt", "")[:500]
    if node_name == "validator":
        vals = state.get("validation_results") or []
        if vals:
            last = vals[-1]
            if hasattr(last, "score"):
                return {"score": last.score, "issues": getattr(last, "issues", [])[:10]}
            if isinstance(last, dict):
                return {"score": last.get("score"), "issues": last.get("issues", [])[:10]}
        return ""
    if node_name == "reflector":
        if state.get("proceed_to_executor") is True:
            return "proceed_to_executor"
        return state.get("sql") or ""
    if node_name == "executor":
        results = state.get("execution_results") or []
        if results:
            return {
                "results_count": len(results),
                "last_columns": getattr(results[-1], "columns", [])[:20] if hasattr(results[-1], "columns") else [],
            }
        return ""
    return ""


def _state_to_meta(state: dict[str, Any]) -> dict[str, Any]:
    """从 state 提取元数据（精简、可序列化）。"""
    if not isinstance(state, dict):
        return {}
    meta: dict[str, Any] = {}
    if "score" in state and state["score"] is not None:
        meta["score"] = state["score"]
    if "retries" in state:
        meta["retries"] = state["retries"]
    if "llm_total_tokens" in state:
        meta["llm_total_tokens"] = state["llm_total_tokens"]
    if "proceed_to_executor" in state:
        meta["proceed_to_executor"] = state["proceed_to_executor"]
    return meta


def _build_trajectory_steps(trajectory: list[Any]) -> list[dict[str, Any]]:
    """将 astream 的 chunk 列表转成按 step 组织的列表。"""
    steps: list[dict[str, Any]] = []
    for i, item in enumerate(trajectory):
        node_name = ""
        state: dict[str, Any] = {}
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            node_name = str(item[0]) if item[0] is not None else ""
            state = item[1] if isinstance(item[1], dict) else {}
        elif isinstance(item, dict):
            state = item
        # 跳过仅首 chunk 且为纯初始 state（无 node、无任何节点产出）的情况
        if (
            i == 0
            and not node_name
            and not state.get("semantic_query")
            and not state.get("schema")
            and not state.get("sql")
        ):
            continue
        step_num = len(steps) + 1
        thinking = (state.get("reasoning") or "").strip()
        action_result = bool(node_name == "reflector" and state.get("proceed_to_executor") is False)
        response = _state_to_response(node_name, state)
        meta = _state_to_meta(state)
        steps.append(
            {
                "step": step_num,
                "thinking": thinking,
                "action": node_name,
                "action_result": action_result,
                "response": response,
                "meta": meta,
            }
        )
    return steps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NL2SQL 单条评测：指定 query、SQLite 路径、输出目录，输出 {db_id}.sql、轨迹、agent_result.json"
    )
    parser.add_argument("--query", "-q", type=str, required=True, help="自然语言问题")
    parser.add_argument(
        "--assert-path",
        "-d",
        "--sqlite-path",
        type=str,
        dest="assert_path",
        required=True,
        help="SQLite 数据库文件路径",
    )
    parser.add_argument("--output-path", "-o", type=str, required=True, help="输出目录")
    parser.add_argument(
        "--db-id",
        type=str,
        default=None,
        help="DATABASE.db_id，不传则从 sqlite 文件名取（无后缀），并去掉 bird_ 前缀",
    )
    parser.add_argument("--config", "-c", type=str, default=None, help="Agent 配置文件 YAML 路径")
    return parser.parse_args()


def get_default_config_path() -> Path:
    return PROJECT_DIR / "dataagent" / "agents" / "nl2sql" / "nl2sql_agent.yaml"


async def run_eval(
    query: str,
    assert_path: str,
    output_path: str,
    db_id: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    out_dir = Path(output_path).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config_path = config_path or str(get_default_config_path())
    if not Path(config_path).exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    stem = Path(assert_path).stem
    if db_id is None:
        db_id = stem[5:] if stem.startswith("bird_") else stem
    assert_path_abs = str(Path(assert_path).resolve())
    if not Path(assert_path_abs).exists():
        raise FileNotFoundError(f"SQLite file not found: {assert_path_abs}")

    # 生成的 SQL 文件名与 db_id 一致
    sql_filename = f"{db_id}.sql"
    sql_file_path = out_dir / sql_filename
    trajectory_path = out_dir / "trajectory.json"
    result_path = out_dir / "agent_result.json"

    agent = DataAgent.from_config(config_path)
    agent.config.set("DATABASE.db_id", db_id)
    db_config = dict(agent.config.get("DATABASE.config", {}) or {})
    db_config["path"] = assert_path_abs
    agent.config.set("DATABASE.config", db_config)

    result_payload: dict[str, Any] = {
        "status": "success",
        "artifacts": {"sql": str(sql_file_path.resolve())},
        "runtime": {
            "e2e_ms": 0.0,
            "llm_ms": 0.0,
            "tool_ms": 0.0,
            "total_tokens": 0,
            "valid_tool_calls": 0,
            "invalid_tool_calls": 0,
        },
        "trace_path": str(trajectory_path.resolve()),
        "error": None,
        "meta": {"query": query[:200], "db_id": db_id, "assert_path": assert_path_abs},
    }

    trajectory: list[Any] = []
    final_state: dict[str, Any] | None = None
    t0 = time.perf_counter()

    try:
        initial_state = get_default_state(query)
        gen = agent.astream(initial_state=initial_state, stream_mode="values")
        async for chunk in gen:
            trajectory.append(chunk)
            if isinstance(chunk, (list, tuple)) and len(chunk) >= 2:
                final_state = chunk[1] if isinstance(chunk[1], dict) else final_state
            elif isinstance(chunk, dict):
                final_state = chunk

        result_payload["runtime"]["e2e_ms"] = (time.perf_counter() - t0) * 1000.0

        if final_state is None and trajectory:
            last = trajectory[-1]
            if isinstance(last, (list, tuple)) and len(last) >= 2 and isinstance(last[1], dict):
                final_state = last[1]

        if final_state is None:
            raw = await agent._chat_agent.chat(query)
            final_state = dict(raw) if isinstance(raw, dict) else {}
            if not trajectory:
                result_payload["runtime"]["e2e_ms"] = (time.perf_counter() - t0) * 1000.0

        sql_text = (final_state or {}).get("sql", "") or ""
        sql_file_path.write_text(sql_text, encoding="utf-8")
        result_payload["runtime"]["total_tokens"] = (final_state or {}).get("llm_total_tokens", 0)

        steps = _build_trajectory_steps(trajectory)
        trajectory_output = {"steps": steps}
        trajectory_path.write_text(
            json.dumps(trajectory_output, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    except Exception as e:
        result_payload["status"] = "failed"
        result_payload["error"] = str(e)
        result_payload["runtime"]["e2e_ms"] = (time.perf_counter() - t0) * 1000.0
        logger.exception("run_nl2sql_single_eval failed")
        sql_file_path.write_text("", encoding="utf-8")
        trajectory_path.write_text(
            json.dumps({"steps": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result_payload


def main() -> None:
    args = parse_args()
    payload = asyncio.run(
        run_eval(
            query=args.query,
            assert_path=args.assert_path,
            output_path=args.output_path,
            db_id=args.db_id,
            config_path=args.config,
        )
    )
    logger.info("agent_result: {}", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
