from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List

try:
    from ... import config
except ImportError:
    import config

from ...common.atomic_io import atomic_write_json
from ..models.step1_models import StepResult
from ..sources.bird_metadata import (
    load_dev_tables,
    load_table_description_map,
    load_table_info,
    select_databases,
    sqlite_path,
)
from .base import StepBuilder

logger = logging.getLogger(__name__)


# === 控制字符清洗（仅供 distinct 实值归一化使用） ===
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class Step1f1ExtractValueEnumBuilder(StepBuilder):
    """列级 enum 提取 builder（LLM-first 策略）。

    入口硬过滤：
      ① CSV 中 value_description 非空（无描述则人类分析师也无法判定枚举语义）；
      ② sqlite distinct 值数量 ≤ STEP1F_ENUM_MAX_DISTINCT（默认 50）。
    满足上述两条的列直接调用 LLM 解析 value_description，得到 {value, description} 列表，
    再用 _validate_enum_pairs 与 sqlite distinct 实际值做事实核对（coverage / unmatched）。

    非枚举列不写任何输出文件，仅 add_completed_key 标记完成，避免重跑重复 LLM 调用。
    LLM 温度：STEP1F_LLM_TEMPERATURE。
    """

    step_name = "step1f1_extract_value_enum"

    def _run_impl(self) -> StepResult:
        dev_tables = load_dev_tables(self.context.bird_tables_json)
        db_entries = select_databases(
            dev_tables,
            db_filter=self.context.db_filter,
            limit=self.context.limit,
        )

        # LLM-first 策略：LLM 为唯一解析路径
        llm_client = self.context.get_llm_client()

        completed = set(self.progress.completed_keys()) if not self.context.force_rerun else set()
        logger.info("step1f1: %d databases to process, %d columns already completed",
                    len(db_entries), len(completed))

        processed_count = 0
        skipped_count = 0
        outputs: List[str] = []

        for db_idx, entry in enumerate(db_entries, 1):
            db_id = str(entry.get("db_id") or "").strip()
            if not db_id:
                continue
            if not self.context.should_process_db(db_id):
                continue

            db_path = sqlite_path(self.context.bird_db_dir, db_id)
            if not db_path.exists():
                raise FileNotFoundError(f"sqlite database not found: {db_path}")

            table_names = [
                str(name or "").strip()
                for name in (entry.get("table_names_original") or [])
                if str(name or "").strip()
            ]

            # 统计当前 db 总列数
            total_cols_in_db = 0
            processed_in_db = 0
            logger.info("step1f1: [%d/%d] db=%s tables=%d, scanning columns...",
                        db_idx, len(db_entries), db_id, len(table_names))

            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                for table_name in table_names:
                    desc_map = load_table_description_map(self.context.bird_db_dir, db_id, table_name)
                    table_info = load_table_info(db_path, table_name)
                    total_cols_in_db += len(table_info)
                    for column in table_info:
                        column_name = str(column.get("name") or "").strip()
                        if not column_name:
                            continue
                        column_id = f"{db_id}.{table_name}.{column_name}"

                        # 续跑路径 1：state 已记录 → 跳过
                        if column_id in completed:
                            skipped_count += 1
                            continue

                        staging_path = self.context.artifacts.step1f1_column_staging_path(
                            db_id, table_name, column_name
                        )

                        # 续跑路径 2：staging 已存在但未登记 → 补登记，跳过 LLM
                        if not self.context.force_rerun and staging_path.exists():
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            outputs.append(str(staging_path))
                            skipped_count += 1
                            continue

                        row = desc_map.get(column_name) or desc_map.get(column_name.lower()) or {}
                        value_description = str((row or {}).get("value_description") or "").strip()
                        if not value_description:
                            # 硬过滤 ①：无 vd 则无枚举语义判据，跳过
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            processed_count += 1
                            continue

                        # 硬过滤 ②：distinct 超限则不是枚举（前置于 LLM 调用之前，避免无谓 LLM 成本）
                        distinct_values = _load_distinct_values(
                            conn,
                            table_name,
                            column_name,
                            limit=int(config.STEP1F_ENUM_MAX_DISTINCT) + 1,
                        )
                        if len(distinct_values) > int(config.STEP1F_ENUM_MAX_DISTINCT):
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            processed_count += 1
                            continue

                        # LLM 主路径：直接调用 LLM 从 value_description 提取 {value, description}
                        parsed_values = _llm_extract_enum_pairs(
                            llm_client, column_name, value_description
                        )
                        if not parsed_values:
                            # LLM 返回空（描述不含枚举映射或调用迫续失败）
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            processed_count += 1
                            continue

                        # 事实核对：防止 LLM 幻觉，要求与 sqlite distinct 实值匹配
                        validated_values, coverage, unmatched_count = _validate_enum_pairs(
                            parsed_values, distinct_values
                        )
                        if len(validated_values) < int(config.STEP1F_ENUM_MIN_MAPPING_SIZE):
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            processed_count += 1
                            continue
                        if (
                            distinct_values
                            and coverage < float(config.STEP1F_ENUM_COVERAGE_THRESHOLD)
                            and unmatched_count > int(config.STEP1F_ENUM_MAX_UNMATCHED)
                        ):
                            self.progress.add_completed_key(column_id)
                            completed.add(column_id)
                            processed_count += 1
                            continue

                        # 校验通过为枚举列：atomic 写 staging → 后 add_completed_key
                        payload: Dict[str, Any] = {
                            "column_id": column_id,
                            "db_id": db_id,
                            "table_name": table_name,
                            "column_name": column_name,
                            "data_type": str(column.get("type") or ""),
                            "coverage": coverage,
                            "distinct_count": len(distinct_values),
                            "unmatched_count": unmatched_count,
                            "values": validated_values,
                        }
                        atomic_write_json(staging_path, payload)
                        outputs.append(str(staging_path))

                        self.progress.add_completed_key(column_id)
                        completed.add(column_id)
                        processed_count += 1
                        processed_in_db += 1

            logger.info("step1f1: [%d/%d] db=%s done, columns_processed=%d",
                        db_idx, len(db_entries), db_id, processed_in_db)

        return StepResult(
            step_name=self.step_name,
            status="success",
            processed_count=processed_count,
            skipped_count=skipped_count,
            outputs=outputs,
        )


# === 辅助函数 ===


def _strip_wrapping_quotes(value: str) -> str:
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"', '`'}:
        text = text[1:-1].strip()
    return text


def _normalize_enum_value(value: Any) -> str:
    text = "" if value is None else str(value)
    text = _CONTROL_CHARS_RE.sub(" ", text).replace("\uFFFD", " ")
    text = _strip_wrapping_quotes(text).strip()
    if bool(config.STEP1F_ENUM_CASE_INSENSITIVE):
        text = text.lower()
    return text


def _load_distinct_values(
    conn: sqlite3.Connection, table_name: str, column_name: str, limit: int
) -> List[str]:
    sql = (
        f"SELECT DISTINCT `{column_name}` FROM `{table_name}` "
        f"WHERE `{column_name}` IS NOT NULL AND CAST(`{column_name}` AS TEXT) != '' LIMIT {int(limit)};"
    )
    rows = conn.execute(sql).fetchall()
    return [str(row[0]) for row in rows if row and row[0] is not None and str(row[0]).strip()]


def _validate_enum_pairs(
    parsed_values: List[Dict[str, str]], distinct_values: List[str]
) -> tuple[List[Dict[str, str]], float, int]:
    mapping: Dict[str, str] = {}
    for item in parsed_values:
        normalized = _normalize_enum_value(item.get("value"))
        description = str(item.get("description") or "").strip()
        if not normalized or not description or normalized in mapping:
            continue
        mapping[normalized] = description
    if not distinct_values:
        return list(parsed_values), 1.0, 0
    ignore_set = {
        _normalize_enum_value(item)
        for item in (config.STEP1F_ENUM_IGNORE_COVERAGE_VALUES or [])
        if str(item).strip()
    }
    actual_values = [_normalize_enum_value(value) for value in distinct_values]
    actual_set = {value for value in actual_values if value and value not in ignore_set}
    if not actual_set:
        return list(parsed_values), 1.0, 0
    matched_values: List[Dict[str, str]] = []
    seen: set[str] = set()
    for raw_value in distinct_values:
        normalized = _normalize_enum_value(raw_value)
        if not normalized or normalized in seen or normalized not in mapping:
            continue
        seen.add(normalized)
        matched_values.append({"value": str(raw_value), "description": mapping[normalized]})
    matched_count = sum(1 for value in actual_set if value in mapping)
    unmatched_count = len(actual_set) - matched_count
    coverage = matched_count / float(len(actual_set)) if actual_set else 1.0
    return matched_values, coverage, unmatched_count


def _extract_json_object(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end >= 0 and end > start:
        return s[start:end + 1]
    return s


def _llm_extract_enum_pairs(llm_client: Any, column_name: str, value_description: str) -> List[Dict[str, str]]:
    """调用失败与 JSON 解析失败共用同一 max_retries 预算。"""
    prompt = (
        "You are given one database column and its value_description text. "
        "Extract enumerated value-description pairs strictly from the given text. "
        "Return ONLY valid JSON with schema {\"pairs\":[{\"value\":string,\"description\":string}]}. "
        "Do not invent values. Only include pairs whose description is non-empty.\n\n"
        f"column_name: {column_name}\n"
        f"value_description: {value_description}"
    )
    eff_max = max(1, int(getattr(config, "LLM_PARSE_FAIL_MAX_RETRIES", 10)))
    eff_delay = float(getattr(config, "LLM_RETRY_DELAY", 2))
    eff_mult = float(getattr(config, "LLM_BACKOFF_MULTIPLIER", 2))
    delay = eff_delay
    payload: Any = None
    last_err: Exception | None = None
    for attempt in range(1, eff_max + 1):
        try:
            response = llm_client.chat(prompt, temperature=float(config.STEP1F_LLM_TEMPERATURE))
            payload = json.loads(_extract_json_object(response))
            break
        except Exception as exc:
            last_err = exc
            logger.warning(
                "_llm_extract_enum_pairs: attempt %d/%d failed for column=%s: %s%s",
                attempt,
                eff_max,
                column_name,
                exc,
                " (retrying...)" if attempt < eff_max else " (giving up, returning [])",
            )
            if attempt < eff_max:
                time.sleep(delay)
                delay *= eff_mult
    if payload is None:
        return []
    pairs = payload.get("pairs") if isinstance(payload, dict) else []
    if not isinstance(pairs, list):
        return []
    normalized_pairs: List[Dict[str, str]] = []
    seen: set[str] = set()
    for item in pairs:
        if not isinstance(item, dict):
            continue
        value = _strip_wrapping_quotes(str(item.get("value") or "").strip())
        description = _strip_wrapping_quotes(str(item.get("description") or "").strip())
        if not value or not description:
            continue
        # 以 normalized value 去重（与后续 _validate_enum_pairs 的去重口径一致）
        normalized = _normalize_enum_value(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        description = re.sub(r"\s+", " ", description).strip()
        if not description:
            continue
        normalized_pairs.append({"value": value, "description": description})
    return normalized_pairs
