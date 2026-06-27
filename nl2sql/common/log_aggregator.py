"""日志聚合工具 -- 按题目分组、combo 分组、SQL 粒度子分组、按时间排序

聚合逻辑：
  1. 逐行读取，用正则提取 [q_(\\d{4})]、[combo:(\\w+)] 和 [(dc|skeleton|icl)/sql_(\\d+)]
  2. 第一级分组：按 question_id（无 qid 的归入 "global"）
  3. 第二级分组（combo）：按 [combo:X] 标签（无 combo 标签时归入 ""）
  4. 第三级分组（题目内部）：
     - 含 [route/sql_N] 标签的行 → 按 route/sql 聚合（生成+校验链路）
     - 含 Selector / full_review / shortcut / single 标签 → 归入 "selector" 子组
     - 其余行 → 归入 "other" 子组
  5. 每组内按原始行顺序（时间戳顺序）保持
  6. 输出时，若题目仅含 combo_key=""，退化为原有扁平格式（step3b 兼容）
"""

import re
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 正则：提取 question_id
_RE_QID = re.compile(r"\[q_(\d{4})\]")
# 正则：提取 combo 标签，如 [combo:C]
_RE_COMBO = re.compile(r"\[combo:(\w+)\]")
# 正则：提取 route/sql 标签，如 [dc/sql_1]
_RE_SQL_TAG = re.compile(r"\[(dc|skeleton|icl)/sql_(\d+)\]")
# 正则：判断 selector 相关日志
_RE_SELECTOR = re.compile(r"\b(Selector|full_review|shortcut|single|sweeper_stage1_no_FR|fallback_no_llm|br_selection|sql_selector)\b", re.IGNORECASE)
# 正则：判断是否为新日志行（以时间戳开头：2026-04-17 14:42:12）
_RE_LOG_LINE_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


def _render_sub_groups(sub_groups: OrderedDict, lines_out: list) -> None:
    """渲染 sub_groups 内容（other → sql 链路 → selector）

    Args:
        sub_groups: { sub_key: [lines] } 字典
        lines_out: 输出行列表（就地追加）
    """
    # 先输出 other（通常包含题目级别的日志，如开始处理、DDL 加载等）
    if "other" in sub_groups:
        lines_out.append("--- General ---")
        for line in sub_groups["other"]:
            lines_out.append(line)
        lines_out.append("")

    # 输出各 SQL 链路（按 key 排序：dc/sql_1, dc/sql_2, skeleton/sql_1...）
    sql_keys = sorted([k for k in sub_groups if k.startswith("sql:")])
    for sql_key in sql_keys:
        tag_display = sql_key.replace("sql:", "")  # "dc/sql_1"
        lines_out.append(f"--- [{tag_display}] ---")
        for line in sub_groups[sql_key]:
            lines_out.append(line)
        lines_out.append("")

    # 输出 selector 日志
    if "selector" in sub_groups:
        lines_out.append("--- Selector ---")
        for line in sub_groups["selector"]:
            lines_out.append(line)
        lines_out.append("")


def aggregate_log(log_file: Path, output_file: Path = None) -> Path:
    """读取原始日志文件，按题目/SQL 粒度聚合。

    Args:
        log_file: 原始日志文件路径
        output_file: 输出路径（默认 {原文件名}_by_question.log）

    Returns:
        聚合后的日志文件路径
    """
    log_file = Path(log_file)
    if output_file is None:
        output_file = log_file.with_name(f"{log_file.stem}_by_question{log_file.suffix}")
    else:
        output_file = Path(output_file)

    # ── 1. 逐行读取 & 分组 ──
    # 三级结构: { qid_str: { combo_key: { sub_group_key: [lines] } } }
    # combo_key: "C" | "A" | "B" | "" (无 combo 标签，step3b 或 sweep decision)
    # sub_group_key: "sql:dc/sql_1" | "selector" | "other"
    groups: OrderedDict[str, OrderedDict[str, OrderedDict[str, list]]] = OrderedDict()
    global_lines: list = []
    question_first_seen: dict = {}  # qid -> line_no（保持出现顺序）
    # 续行上下文：不以时间戳开头的行继承前一行的 qid、combo、sub_key
    prev_qid: str | None = None
    prev_combo: str | None = None
    prev_sub_key: str | None = None

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            line = line.rstrip("\n")

            # 判断是否为新日志行（以时间戳开头）
            is_new_log_line = bool(_RE_LOG_LINE_START.match(line))

            qid_match = _RE_QID.search(line)
            if not qid_match:
                if not is_new_log_line and prev_qid is not None:
                    # 续行：继承前一行的 qid、combo、sub_key
                    combo_groups = groups[prev_qid]
                    sub_groups = combo_groups[prev_combo]
                    if prev_sub_key not in sub_groups:
                        sub_groups[prev_sub_key] = []
                    sub_groups[prev_sub_key].append(line)
                else:
                    # 真正的全局行（有时间戳但无 qid，或首行无上下文）
                    global_lines.append(line)
                    prev_qid = None
                    prev_combo = None
                    prev_sub_key = None
                continue

            qid_str = f"q_{qid_match.group(1)}"

            # 确保 qid 按首次出现顺序排列
            if qid_str not in groups:
                groups[qid_str] = OrderedDict()
                question_first_seen[qid_str] = line_no

            combo_groups = groups[qid_str]

            # 提取 combo 标签
            combo_match = _RE_COMBO.search(line)
            combo_key = combo_match.group(1) if combo_match else ""

            if combo_key not in combo_groups:
                combo_groups[combo_key] = OrderedDict()
            sub_groups = combo_groups[combo_key]

            # 判断子分组
            sql_tag_match = _RE_SQL_TAG.search(line)
            if sql_tag_match:
                route = sql_tag_match.group(1)
                sql_idx = sql_tag_match.group(2)
                sub_key = f"sql:{route}/sql_{sql_idx}"
            elif _RE_SELECTOR.search(line):
                sub_key = "selector"
            else:
                sub_key = "other"

            if sub_key not in sub_groups:
                sub_groups[sub_key] = []
            sub_groups[sub_key].append(line)

            # 记录当前上下文，供续行继承
            prev_qid = qid_str
            prev_combo = combo_key
            prev_sub_key = sub_key

    # ── 2. 生成输出 ──
    lines_out = []

    # 题目执行概览（在文件开头）
    lines_out.append("=" * 60)
    lines_out.append("题目执行概览")
    lines_out.append("=" * 60)
    lines_out.append(f"Total questions: {len(groups)}")
    lines_out.append("")

    for qid_str, combo_groups in groups.items():
        total_lines = sum(
            len(lines)
            for sub_groups in combo_groups.values()
            for lines in sub_groups.values()
        )
        # 统计命名 combo 数（排除 "" 空 combo）
        named_combos = [k for k in combo_groups if k != ""]
        combo_count = len(named_combos)

        # 统计所有 combo 下的 SQL 数量
        sql_count = sum(
            1
            for sub_groups in combo_groups.values()
            for k in sub_groups if k.startswith("sql:")
        )

        # 判断 selector 策略（优先从命名 combo 中查找，否则从 "" 中查找）
        selector_strategy = "N/A"
        for combo_key in list(combo_groups.keys()):
            sub_groups = combo_groups[combo_key]
            if "selector" in sub_groups:
                for sl in sub_groups["selector"]:
                    strategy_match = re.search(r"strategy=(\w+)", sl)
                    if strategy_match:
                        selector_strategy = strategy_match.group(1)
                        break
                if selector_strategy != "N/A":
                    break

        # 概览行：有 combo 时显示 combo 数
        if combo_count > 0:
            lines_out.append(
                f"  [{qid_str}]: {total_lines} entries "
                f"({combo_count} combos, {sql_count} SQLs validated, selector: {selector_strategy})"
            )
        else:
            lines_out.append(
                f"  [{qid_str}]: {total_lines} entries "
                f"({sql_count} SQLs validated, selector: {selector_strategy})"
            )

    lines_out.append("")

    # 逐题目输出
    for qid_str, combo_groups in groups.items():
        total_lines = sum(
            len(lines)
            for sub_groups in combo_groups.values()
            for lines in sub_groups.values()
        )
        lines_out.append("=" * 60)
        lines_out.append(f"[{qid_str}] 执行轨迹 (共 {total_lines} 条日志)")
        lines_out.append("=" * 60)
        lines_out.append("")

        named_combos = [k for k in combo_groups if k != ""]
        has_combos = len(named_combos) > 0

        if not has_combos:
            # 扁平模式（step3b 兼容）：只有 combo_key=""
            sub_groups = combo_groups.get("", OrderedDict())
            _render_sub_groups(sub_groups, lines_out)
        else:
            # combo 模式：先输出各命名 combo，最后输出 "" (sweep decision)
            for combo_key in combo_groups:
                if combo_key == "":
                    continue  # 最后输出
                sub_groups = combo_groups[combo_key]
                combo_lines = sum(len(v) for v in sub_groups.values())
                lines_out.append(f"\u2501\u2501\u2501 [combo:{combo_key}] ({combo_lines} 条) "
                                 + "\u2501" * 38)
                lines_out.append("")
                _render_sub_groups(sub_groups, lines_out)

            # 输出无 combo 标签的日志（sweep decision 等题目级决策日志）
            if "" in combo_groups:
                decision_subs = combo_groups[""]
                if any(decision_subs.values()):
                    lines_out.append("--- Sweep Decision ---")
                    # 决策日志通常在 "other" 和 "selector" 子组
                    for sub_key in decision_subs:
                        for line in decision_subs[sub_key]:
                            lines_out.append(line)
                    lines_out.append("")

    # 全局日志
    if global_lines:
        lines_out.append("=" * 60)
        lines_out.append(f"[Global] 非题目日志 (共 {len(global_lines)} 条)")
        lines_out.append("=" * 60)
        lines_out.append("")
        for line in global_lines:
            lines_out.append(line)
        lines_out.append("")

    # ── 3. 写入文件 ──
    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines_out))

    logger.info(f"Aggregated log written to: {output_file} "
                f"({len(groups)} questions, {len(global_lines)} global lines)")
    return output_file
