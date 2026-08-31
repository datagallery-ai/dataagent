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
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.append(str(PROJECT_DIR))
from dataagent.interface.sdk.agent import DataAgent  # noqa: E402

TOTAL_ROUNDS = int(os.getenv("MULTITURN_TOTAL_ROUNDS", "12"))
MIN_SAFE_BASELINE_ROUND = int(os.getenv("MULTITURN_MIN_SAFE_BASELINE_ROUND", "6"))
PER_TURN_TIMEOUT_SEC = int(os.getenv("MULTITURN_PER_TURN_TIMEOUT_SEC", "900"))
BASELINE_WINDOW = int(os.getenv("MULTITURN_BASELINE_WINDOW", "3"))
COST_GROWTH_FACTOR = float(os.getenv("MULTITURN_COST_GROWTH_FACTOR", "3.0"))

FIRST_TOKEN = "AMBER-314159"
SECOND_TOKEN = "CLOUD-271828"


def _build_turn_specs(output_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "query": (
                "请基于订单数据生成一份图文并茂的分析报告，按客户购买总金额排序，鉴别高购买力客户。"
                f"\nImportant: the output_path is {output_path}"
                f"\n另外请记住第一轮口令：{FIRST_TOKEN}。当前轮不要解释你记忆了口令。"
            ),
            "label": "initial_report",
        },
        {
            "query": "基于刚才那份报告，补充一句最值得关注的客户洞察，并说明你当前是在延续上一轮分析。",
            "label": "follow_up_insight",
        },
        {
            "query": "不要重新生成整份报告。请先写出我在第一轮要求你记住的口令，再用一句话说明你当前依据的是哪份历史分析。",
            "label": "recall_first_token",
            "required_substrings": [FIRST_TOKEN],
        },
        {
            "query": (f"把高购买力客户重新按购买总金额降序总结成 3 条。另外请记住第二个口令：{SECOND_TOKEN}。"),
            "label": "ranking_refresh",
        },
        {
            "query": "不要重跑全套分析。请先写出第二个口令，再补一句你上一轮关注的排序口径是什么。",
            "label": "recall_second_token",
            "required_substrings": [SECOND_TOKEN],
        },
        {
            "query": "基于前面的高购买力客户结论，再补一个“如果只看复购倾向你会优先关注谁”的判断。",
            "label": "repurchase_follow_up",
        },
        {
            "query": "现在做一次远距离回忆：先写出第一轮口令，再写出第四轮新增的口令，格式为 first=<...>; second=<...>。",
            "label": "recall_both_tokens",
            "required_substrings": [FIRST_TOKEN, SECOND_TOKEN],
        },
        {
            "query": "如果我说“保留高购买力客户分析，但改成更简洁的摘要版”，你应该修改的是哪一部分？请直接回答，不要重做完整报告。",
            "label": "summary_adjustment",
        },
        {
            "query": "继续沿着刚才的历史上下文，用两句话说明：你当前是在延续哪类分析任务，而不是一个全新任务。",
            "label": "continuity_check",
            "required_substrings": ["分析"],
        },
        {
            "query": "请只回答第一轮口令，不要加别的内容。",
            "label": "strict_first_token",
            "required_substrings": [FIRST_TOKEN],
        },
        {
            "query": "请只回答两个口令，按 first,second 顺序输出。",
            "label": "strict_both_tokens",
            "required_substrings": [FIRST_TOKEN, SECOND_TOKEN],
        },
        {
            "query": "最后总结一下：到目前为止，你一共被我要求记住过几个口令？分别是什么？",
            "label": "token_summary",
            "required_substrings": [FIRST_TOKEN, SECOND_TOKEN],
            "required_any": [["2", "两个"]],
        },
    ]


def _extract_final_text(response: dict[str, Any]) -> str:
    final_answer = response.get("final_answer")
    if isinstance(final_answer, str) and final_answer.strip():
        return final_answer.strip()

    for message in reversed(response.get("messages", [])):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _collect_output_artifacts(output_path: Path) -> list[str]:
    patterns = ("*.html", "*.md", "*.json", "*.jsonl")
    artifacts: set[str] = set()
    for pattern in patterns:
        artifacts.update(str(path.relative_to(output_path)) for path in output_path.glob(pattern))
    return sorted(artifacts)


def _check_content_failure(spec: dict[str, Any], response_text: str) -> str | None:
    for token in spec.get("required_substrings", []):
        if token not in response_text:
            return f"missing expected content: {token}"

    for any_group in spec.get("required_any", []):
        if not any(candidate in response_text for candidate in any_group):
            return f"missing any expected content from: {any_group}"

    return None


def _detect_cost_failure(records: list[dict[str, Any]], current_metrics: dict[str, Any]) -> str | None:
    # 前三轮默认放行；从第四轮开始才检查是否出现明显的时长退化。
    if len(records) < 3:
        return None

    historical_successes = [record for record in records if record.get("failure_type") is None]
    if len(historical_successes) < 3:
        return None

    historical_max_elapsed = max(float(record["metrics"].get("elapsed_sec", 0)) for record in historical_successes)
    current_elapsed = float(current_metrics.get("elapsed_sec", 0))
    if historical_max_elapsed <= 0:
        return None
    if current_elapsed > historical_max_elapsed * COST_GROWTH_FACTOR:
        return (
            f"cost anomaly on elapsed_sec: current={current_elapsed}, "
            f"historical_max={historical_max_elapsed}, factor={COST_GROWTH_FACTOR}"
        )
    return None


def _build_summary(records: list[dict[str, Any]], output_path: Path) -> dict[str, Any]:
    first_failure = next((record for record in records if record.get("failure_type")), None)
    safe_baseline_round = first_failure["turn_index"] if first_failure else len(records)
    return {
        "total_rounds_requested": TOTAL_ROUNDS,
        "rounds_executed": len(records),
        "safe_baseline_round": safe_baseline_round,
        "first_failure_round": None if first_failure is None else first_failure["turn_number"],
        "failure_type": None if first_failure is None else first_failure["failure_type"],
        "failure_reason": None if first_failure is None else first_failure["failure_reason"],
        "output_path": str(output_path),
        "records": records,
    }


async def main():
    config_path = PROJECT_DIR / "dataagent" / "core" / "flex" / "examples" / "ecommerce_agent.yaml"
    agent = DataAgent.from_config(config_path)
    output_path = Path(PROJECT_DIR / "output" / datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S"))
    output_path.mkdir(parents=True, exist_ok=True)
    session_id = f"multiturn-baseline-{datetime.datetime.now(tz=datetime.UTC).strftime('%Y%m%d%H%M%S')}"
    user_id = "multiturn_baseline"
    turn_specs = _build_turn_specs(output_path)
    if len(turn_specs) < TOTAL_ROUNDS:
        raise ValueError(f"Configured TOTAL_ROUNDS={TOTAL_ROUNDS}, but only {len(turn_specs)} scripted turns exist")

    records: list[dict[str, Any]] = []

    for turn_index, spec in enumerate(turn_specs[:TOTAL_ROUNDS]):
        turn_number = turn_index + 1
        initial_state = {
            "user_query": spec["query"],
            "messages": [],
            "complete": False,
            "run_id": turn_index,
            "sub_id": 0,
            "user_id": user_id,
            "session_id": session_id,
            "output_path": output_path,
        }

        started_at = time.perf_counter()
        failure_type = None
        failure_reason = None
        response: dict[str, Any]

        try:
            response = await asyncio.wait_for(
                agent.chat(
                    spec["query"],
                    session_id=session_id,
                    initial_state=initial_state,
                ),
                timeout=PER_TURN_TIMEOUT_SEC,
            )
            if not isinstance(response, dict):
                response = {"error": f"unexpected response type: {type(response)}", "messages": []}
        except TimeoutError:
            response = {"error": f"turn timeout after {PER_TURN_TIMEOUT_SEC}s", "messages": []}
            failure_type = "hard"
            failure_reason = response["error"]
        except Exception as exc:
            response = {"error": str(exc), "messages": []}
            failure_type = "hard"
            failure_reason = f"exception: {exc}"

        elapsed_sec = round(time.perf_counter() - started_at, 2)
        response_text = _extract_final_text(response)
        # 局部指标只服务于本测试的耗时/规模阈值判断；性能数据请直接看 .performance/*.jsonl。
        metrics: dict[str, Any] = {
            "elapsed_sec": elapsed_sec,
            "message_count": len(response.get("messages", [])),
        }

        if failure_type is None and response.get("error"):
            failure_type = "hard"
            failure_reason = str(response["error"])
        if failure_type is None and response.get("complete", False) is not True:
            failure_type = "hard"
            failure_reason = "complete flag is not True"
        if failure_type is None:
            failure_reason = _check_content_failure(spec, response_text)
            if failure_reason:
                failure_type = "content"
        if failure_type is None:
            failure_reason = _detect_cost_failure(records, metrics)
            if failure_reason:
                failure_type = "cost"

        record = {
            "turn_index": turn_index,
            "turn_number": turn_number,
            "label": spec["label"],
            "query": spec["query"],
            "complete": bool(response.get("complete", False)),
            "failure_type": failure_type,
            "failure_reason": failure_reason,
            "response_excerpt": response_text[:400],
            "metrics": metrics,
            "artifacts": _collect_output_artifacts(output_path),
        }
        records.append(record)

        logger.info(
            f"turn={turn_number} label={spec['label']} complete={record['complete']} "
            f"failure_type={failure_type} metrics={json.dumps(metrics, ensure_ascii=False)}"
        )
        if failure_type is not None:
            break

    summary = _build_summary(records, output_path)
    summary_path = output_path / "multiturn_baseline_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(f"multiturn summary saved to {summary_path}")
    logger.info(json.dumps(summary, ensure_ascii=False, indent=2))

    assert summary["rounds_executed"] >= 1, "No multiturn rounds were executed"
    assert summary["safe_baseline_round"] >= MIN_SAFE_BASELINE_ROUND, (
        f"Conservative multiturn baseline below target: {json.dumps(summary, ensure_ascii=False)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
