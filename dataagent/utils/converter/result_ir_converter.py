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
ResultIRConverter — 两条独立Pipeline将工具执行结果转换为 IR 节点。

Pipeline 1（内容 IR）：从工具返回值和入参中提取内存中的结构化数据
  - TableNode + ColumnNode  — DataFrame 对象或 columns + data 模式
  - ScriptNode             — 入参中的 sql/code/command/script
  - 结构化 IR              — result 中的 table/column/tool 条目
  - FileNode               — 长文本兜底（落盘到 workspace 文件）

Pipeline 2（文件 IR）：通过 workspace 快照差集发现新增/变更文件
  - TableNode  — 表格扩展名文件
  - ScriptNode — 脚本扩展名文件（读取内容）
  - FileNode   — 其它文件
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from dataagent.core.context.context import Context
from dataagent.core.context.utils_context_filesystem import lineage_path_key, md5_file
from dataagent.utils.constants import (
    DEFAULT_IR_COLUMN_SAMPLE_ROWS,
    DEFAULT_IR_COLUMN_UNIQUE_SAMPLES,
    DEFAULT_IR_KNOWLEDGE_MIN_LENGTH,
    DEFAULT_IR_MAX_FILE_CHARS,
    DEFAULT_IR_MAX_PATH_LEN,
)
from dataagent.utils.converter.ir_converter_constants import (
    EXT_SCRIPT_TYPE_MAP,
    SCRIPT_TYPE_MAP,
    TABLE_FILE_EXTS,
    TABLE_INDICATOR_KEYS,
)
from dataagent.utils.messages_utils import write_result_to_workspace


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_read_file(path: Path, max_chars: int = DEFAULT_IR_MAX_FILE_CHARS, workspace: Path | None = None) -> str:
    """安全读取文件内容，限制最大字符数。"""
    # Result IR file previews must not read outside the workspace.
    if workspace is not None and not _path_is_relative_to(path, workspace):
        return f"[unable to read file: {path}]"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n... [truncated, total {len(text)} chars]"
        return text
    except Exception:
        return f"[unable to read file: {path}]"


def _result_to_text(result: Any) -> str:
    """将任意类型的工具结果转为字符串。"""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        try:
            return json.dumps(result, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(result)
    return str(result) if result is not None else ""


_VISIBLE_RESULT_UNSET = object()


def _visible_result_to_text(result: Any, visible_result: Any = _VISIBLE_RESULT_UNSET) -> str:
    """返回模型实际可见的文本，用于长文本 File fallback 阈值检测。

    优先级：
    1. Executor 显式传入的 visible_result（= ToolMessage.content 成功分支，最权威）；
    2. result 本身是标准工具返回 dict 时，取其 original_msg（兼容直接构造 result 的调用方）；
    3. 退回 _result_to_text(result)。
    """
    if visible_result is not _VISIBLE_RESULT_UNSET:
        return _result_to_text(visible_result)
    if isinstance(result, dict) and "original_msg" in result:
        return _result_to_text(result.get("original_msg"))
    return _result_to_text(result)


def _persist_dataframe(
    df: Any,
    tool_name: str,
    df_label: str,
    workspace: Path | None = None,
) -> str:
    """将 DataFrame 持久化到 workspace（若无则 fallback 到临时目录），返回绝对路径。"""
    safe_label = df_label.replace("/", "_").replace(".", "_")
    try:
        if workspace:
            path = workspace / f"ir_{tool_name}_{safe_label}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            import os
            import tempfile

            fd, path_str = tempfile.mkstemp(suffix=".csv", prefix=f"ir_{tool_name}_{safe_label}_")
            os.close(fd)
            path = Path(path_str)
        df.to_csv(str(path), index=False)
        return str(path.resolve())
    except Exception as e:
        logger.warning(f"IR converter: failed to persist DataFrame '{df_label}': {e}")
        return ""


def _find_dataframes(obj: Any, prefix: str = "result") -> list[tuple[str, Any]]:
    """递归搜索对象中的 pd.DataFrame 实例。返回 [(标识名, df), ...]。"""
    try:
        import pandas as pd  # pyright: ignore[reportMissingTypeStubs]
    except ImportError:
        return []

    found: list[tuple[str, Any]] = []
    if isinstance(obj, pd.DataFrame):
        found.append((prefix, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, pd.DataFrame):
                found.append((f"{prefix}.{k}", v))
            elif isinstance(v, (dict, list)):
                found.extend(_find_dataframes(v, f"{prefix}.{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            if isinstance(v, pd.DataFrame):
                found.append((f"{prefix}[{i}]", v))
            elif isinstance(v, (dict, list)):
                found.extend(_find_dataframes(v, f"{prefix}[{i}]"))
    return found


def _to_existing_file_path(raw_path: str, workspace: Path | None) -> str | None:
    if not raw_path or not isinstance(raw_path, str):
        return None
    text = raw_path.strip()
    if not text or len(text) > DEFAULT_IR_MAX_PATH_LEN:
        return None
    if "\n" in text or "\r" in text:
        return None
    if text.startswith(("http://", "https://", "data:")):
        return None

    base = Path(text).expanduser()
    workspace_root: Path | None = None
    if workspace:
        try:
            # Resolve all tool-provided file paths against the workspace root.
            workspace_root = workspace.expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None

    candidates = [base]
    if workspace_root:
        candidates = [base] if base.is_absolute() else [workspace_root / base]

    for cand in candidates:
        try:
            resolved = cand.resolve()
            if workspace_root is not None and not _path_is_relative_to(resolved, workspace_root):
                continue
            if resolved.is_file():
                return str(resolved)
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _extract_file_paths_from_args(tool_args: dict[str, Any], workspace: Path | None) -> set[str]:
    """递归扫描参数中的 str / list[str]，提取存在的文件路径（绝对路径）。"""
    found: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
            return

        if isinstance(obj, (list, tuple, set)):
            for item in obj:
                walk(item)
            return

        if not isinstance(obj, str):
            return

        normalized = _to_existing_file_path(obj, workspace)
        if normalized:
            found.add(normalized)

    walk(tool_args)
    return found


_KNOWN_PATHS_KEY = "_ir_known_data_paths"
_READ_FILE_RECORDS_KEY = "_ir_read_file_records"
_PATH_BEARING_NODE_TYPES = {"File", "Table", "Script", "Skill"}


def _get_known_data_paths(context: Context) -> set[str]:
    """获取 / 懒初始化已被数据节点使用的文件路径索引，存储在 context.messages 中。

    首次调用时扫描当前 + 历史 trajectory 构建索引，后续调用 O(1)。
    """
    existing = context.state.messages.get(_KNOWN_PATHS_KEY)
    if isinstance(existing, set):
        return existing

    used: set[str] = set()

    def _scan_graph(graph) -> None:
        for _, attrs in graph.nodes(data=True):
            if attrs.get("node_type") not in _PATH_BEARING_NODE_TYPES:
                continue
            path_val = attrs.get("path")
            if not isinstance(path_val, str) or not path_val:
                continue
            try:
                used.add(str(Path(path_val).resolve()))
            except (OSError, RuntimeError, ValueError):
                used.add(path_val)

    _scan_graph(context.get_trajectory())
    for hist_graph in context.get_all_historical_trajectories().values():
        _scan_graph(hist_graph)

    context.state.messages[_KNOWN_PATHS_KEY] = used
    return used


def _get_read_file_records(context: Context) -> dict[str, tuple[str, str]]:
    # Cache read_file lineage separately so content changes can create new nodes.
    existing = context.state.messages.get(_READ_FILE_RECORDS_KEY)
    if isinstance(existing, dict):
        return existing
    records = dict(context.get_recorded_files())
    context.state.messages[_READ_FILE_RECORDS_KEY] = records
    return records


class ResultIRConverter:
    """两条Pipeline将工具执行结果转换为 IR 节点：内容Pipeline + 文件Pipeline。"""

    @classmethod
    def convert(
        cls,
        context: Context,
        tool_name: str,
        tool_call_id: str,
        tool_args: dict[str, Any],
        result: Any,
        action_node_label: str,
        workspace: str | Path | None = None,
        pre_existing_files: dict[str, float] | None = None,
        knowledge_min_length: int = DEFAULT_IR_KNOWLEDGE_MIN_LENGTH,
        visible_result: Any = _VISIBLE_RESULT_UNSET,
    ) -> list[str]:
        """
        将工具执行结果转换为 IR 节点，挂载到 ActionNode 下游。

        两条独立Pipeline：
        - Pipeline 1（内容）：从 result / tool_args 提取内存中的结构化数据
        - Pipeline 2（文件）：通过 workspace 快照差集发现新增或修改的文件
        - Pipeline 3（read_file）：校验read file工具读取的文件是否存在context中

        Args:
            pre_existing_files: 工具执行前的 workspace 快照 {绝对路径: mtime}。
            knowledge_min_length: 文件落盘兜底的最小字符阈值，默认使用
                DEFAULT_IR_KNOWLEDGE_MIN_LENGTH。
            visible_result: 模型实际可见的工具返回文本（即 ToolMessage.content 的成功分支），
                由 Executor 传入：original_msg 优先，否则 output_text。提供时长文本 File
                fallback 只用该内容做阈值检测，不用 result / frontend_msg / data。
                错误执行时 Executor 传空串以禁用落盘。

        Pipeline 1 产出的文件路径（如 DataFrame 持久化路径、结构化 IR 的 path 字段）
        传给Pipeline 2 跳过，避免重复建节点。
        """
        workspace_path = Path(workspace).expanduser().resolve() if workspace else None

        content_created, content_paths = cls._content_pipeline(
            context,
            tool_name,
            tool_args,
            result,
            action_node_label,
            workspace_path,
            knowledge_min_length=knowledge_min_length,
            visible_result=visible_result,
        )

        if content_paths:
            _get_known_data_paths(context).update(content_paths)

        file_created = cls._file_pipeline(
            context, tool_name, tool_args, action_node_label, workspace_path, pre_existing_files, content_paths
        )
        file_newly_read = cls._read_file_pipeline(context, tool_name, tool_args, action_node_label, workspace_path)
        return content_created + file_created + file_newly_read

    @classmethod
    def snapshot_dir(cls, directory: str | Path | None) -> dict[str, float]:
        """递归拍摄目录文件快照，返回 {绝对路径: mtime} 映射。

        仅跳过相对 ``directory`` 根路径中以 ``.`` 开头的路径段
        （如 ``.context`` / ``.dataagent/tool_outputs``），避免把
        ``DATAAGENT_HOME=~/.dataagent`` 这类 home 前缀误判为隐藏目录。
        """
        if not directory or not Path(directory).is_dir():
            return {}
        root = Path(directory).resolve()
        snapshot: dict[str, float] = {}
        for f in root.rglob("*"):
            try:
                rel_parts = f.relative_to(root).parts
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel_parts):
                continue
            if f.is_file():
                try:
                    snapshot[str(f.resolve())] = f.stat().st_mtime
                except OSError as e:
                    logger.debug(f"IR converter: snapshot_dir skipped {f}: {e}")
        return snapshot

    # ── Pipeline 1：内容 IR ──────────────────────────────────────────

    @classmethod
    def _content_pipeline(
        cls,
        context: Context,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
        action_node_label: str,
        workspace: Path | None,
        knowledge_min_length: int = DEFAULT_IR_KNOWLEDGE_MIN_LENGTH,
        visible_result: Any = _VISIBLE_RESULT_UNSET,
    ) -> tuple[list[str], set[str]]:
        """内容Pipeline：从 result 和 tool_args 提取结构化内容，创建 IR 节点。

        Returns:
            (created_labels, content_paths):
            创建的节点 label 列表 + 内容Pipeline已关联的文件路径（供Pipeline 2 跳过）
        """
        created: list[str] = []
        content_paths: set[str] = set()

        # 1) DataFrame → TableNode + ColumnNode（持久化到 workspace）
        created += cls._create_dataframe_nodes(context, result, action_node_label, tool_name, workspace, content_paths)

        # 2) columns + data 内存表格 → TableNode + ColumnNode
        created += cls._create_in_memory_table_nodes(context, result, action_node_label, tool_name)

        # 3) 入参中的内联脚本 → ScriptNode
        created += cls._create_script_from_args(context, tool_args, action_node_label, tool_name)

        # 4) 结构化 IR 条目 (table/column/tool)
        created += cls._create_structured_ir(context, result, action_node_label, tool_name, content_paths, workspace)

        # 5) 文件落盘兜底 — 始终尝试，因为入参 IR（如 ScriptNode）和结果 IR（如 stdout）是正交的
        created += cls._create_file_fallback(
            context,
            result,
            action_node_label,
            tool_name,
            workspace,
            knowledge_min_length=knowledge_min_length,
            visible_result=visible_result,
        )

        return created, content_paths

    # ── Pipeline 2：文件 IR ──────────────────────────────────────────

    @classmethod
    def _file_pipeline(
        cls,
        context: Context,
        tool_name: str,
        tool_args: dict[str, Any],
        action_node_label: str,
        workspace: Path | None,
        pre_existing_files: dict[str, float] | None,
        content_paths: set[str],
    ) -> list[str]:
        """文件Pipeline：workspace 变更文件 + 参数引用文件，共同补齐文件类 IR。"""
        created: list[str] = []
        known_paths = _get_known_data_paths(context)

        # Step A: workspace 快照差集（新增/修改文件）
        if workspace and pre_existing_files is not None:
            post_files = cls.snapshot_dir(workspace)
            changed_files: list[str] = []
            for fpath, mtime in post_files.items():
                if fpath in content_paths:
                    continue
                old_mtime = pre_existing_files.get(fpath)
                if old_mtime is None or mtime > old_mtime:
                    changed_files.append(fpath)
            changed_files.sort()

            for fpath in changed_files:
                p = Path(fpath)
                ext = p.suffix.lower()

                if ext in TABLE_FILE_EXTS:
                    label = cls._register_table_node(context, action_node_label, tool_name, fpath)
                elif ext in EXT_SCRIPT_TYPE_MAP:
                    label = cls._register_script_node(
                        context,
                        action_node_label,
                        tool_name,
                        script_content=_safe_read_file(p, workspace=workspace),
                        script_type=EXT_SCRIPT_TYPE_MAP[ext],
                        path=fpath,
                    )
                else:
                    label = cls._register_file_node(context, action_node_label, tool_name, path=fpath)

                if label:
                    created.append(label)
                    known_paths.add(fpath)

        # Step B: 参数引用文件补录（例如 write_file / file_saver 的 path）
        # 按扩展名分流，与 Step A / read_file 一致，避免 .csv 被误建成 FileNode。
        if tool_name == "read_file":
            return created
        arg_paths = _extract_file_paths_from_args(tool_args, workspace)
        for fpath in sorted(arg_paths):
            if fpath in known_paths:
                continue
            p = Path(fpath)
            ext = p.suffix.lower()
            if ext in TABLE_FILE_EXTS:
                label = cls._register_table_node(context, action_node_label, tool_name, fpath)
            elif ext in EXT_SCRIPT_TYPE_MAP:
                label = cls._register_script_node(
                    context,
                    action_node_label,
                    tool_name,
                    script_content=_safe_read_file(p, workspace=workspace),
                    script_type=EXT_SCRIPT_TYPE_MAP[ext],
                    path=fpath,
                )
            else:
                label = cls._register_file_node(context, action_node_label, tool_name, path=fpath)
            if label:
                created.append(label)
                known_paths.add(fpath)

        return created

    # ── Pipeline 3：read_file校验 ──────────────────────────────────────────

    @classmethod
    def _read_file_pipeline(
        cls,
        context: Context,
        tool_name: str,
        tool_args: dict[str, Any],
        action_node_label: str,
        workspace: Path | None,
    ) -> list[str]:
        """read_file校验：校验read file工具读取的文件是否存在context中"""
        if tool_name != "read_file":
            return []

        path = tool_args.get("path")
        if not path:
            return []

        resolved_path = _to_existing_file_path(path, workspace)
        if not resolved_path:
            return []
        p = Path(resolved_path)

        context_recorded_files = _get_read_file_records(context)
        path_key = lineage_path_key(p=str(p))
        current_md5 = md5_file(p=str(p))
        if path_key in context_recorded_files and current_md5 == context_recorded_files[path_key][1]:
            context.add_edge_manually(
                from_node=action_node_label, to_node=context_recorded_files[path_key][0], edge_type="refers_to"
            )
            return []

        ext = p.suffix.lower()
        if ext in TABLE_FILE_EXTS:
            label = cls._register_table_node(context, action_node_label, tool_name, str(p))
        elif ext in EXT_SCRIPT_TYPE_MAP:
            label = cls._register_script_node(
                context,
                action_node_label,
                tool_name,
                script_content=_safe_read_file(p, workspace=workspace),
                script_type=EXT_SCRIPT_TYPE_MAP[ext],
                path=str(p),
            )
        else:
            label = cls._register_file_node(context, action_node_label, tool_name, path=str(p))

        if label:
            context_recorded_files[path_key] = (label, current_md5)
            return [label]
        else:
            return []

    # ── Pipeline 1 子步骤 ────────────────────────────────────────────

    @classmethod
    def _create_dataframe_nodes(
        cls,
        context: Context,
        result: Any,
        action_node_label: str,
        tool_name: str,
        workspace: Path | None,
        content_paths: set[str],
    ) -> list[str]:
        """从 result 中提取 DataFrame 对象，持久化并创建 TableNode + ColumnNode。"""
        created: list[str] = []
        for df_label, df_obj in _find_dataframes(result):
            table_path = _persist_dataframe(df_obj, tool_name, df_label, workspace)
            if table_path:
                content_paths.add(table_path)

            table_label = cls._register_table_node(context, action_node_label, tool_name, table_path)
            if not table_label:
                continue
            created.append(table_label)
            from_table = table_path or df_label
            cols = getattr(df_obj, "columns", None)
            data_rows = df_obj.to_dict("records") if hasattr(df_obj, "to_dict") else []
            created += cls._create_columns_for_table(context, table_label, from_table, cols, data_rows)

        return created

    @classmethod
    def _create_in_memory_table_nodes(
        cls,
        context: Context,
        result: Any,
        action_node_label: str,
        tool_name: str,
    ) -> list[str]:
        """检测 result 中的 columns + data 内存表格模式，创建 TableNode + ColumnNode。"""
        if not isinstance(result, dict) or not TABLE_INDICATOR_KEYS.issubset(result.keys()):
            return []

        table_label = cls._register_table_node(context, action_node_label, tool_name, "")
        if not table_label:
            return []

        created = [table_label]
        created += cls._create_columns_for_table(
            context, table_label, "result", result.get("columns"), result.get("data", [])
        )
        return created

    @classmethod
    def _create_script_from_args(
        cls,
        context: Context,
        tool_args: dict[str, Any],
        action_node_label: str,
        tool_name: str,
    ) -> list[str]:
        """从工具入参中提取内联脚本（sql / code / command / script），创建 ScriptNode。"""
        for key, script_type in SCRIPT_TYPE_MAP.items():
            content = tool_args.get(key)
            if not content or not isinstance(content, str) or not content.strip():
                continue
            label = cls._register_script_node(
                context,
                action_node_label,
                tool_name,
                script_content=content.strip(),
                script_type=script_type,
            )
            return [label] if label else []
        return []

    @classmethod
    def _create_structured_ir(
        cls,
        context: Context,
        result: Any,
        action_node_label: str,
        tool_name: str,
        content_paths: set[str],
        workspace: Path | None,
    ) -> list[str]:
        """从 result 中提取结构化 IR 条目 (table/column/tool)，创建对应节点。

        兼容 Executor 的 unwrap：先在当前层查找 table/column/tool，
        若不存在再尝试 result["data"] 层。
        """
        if not isinstance(result, dict):
            return []

        source = result
        if "table" not in source and "column" not in source and "tool" not in source:
            data = result.get("data")
            if isinstance(data, dict):
                source = data
            else:
                return []

        created: list[str] = []
        table_id_to_label: dict[str, str] = {}

        # 1) TableNode
        for entry in source.get("table") or []:
            if not isinstance(entry, dict) or "label" not in entry:
                continue
            path_val = entry.get("path", entry["label"])
            if workspace and str(path_val) != str(entry["label"]):
                path_val = _to_existing_file_path(str(path_val), workspace) or entry["label"]
            try:
                tbl_label = context.register_node(
                    node_type="Table",
                    label=entry["label"],
                    description=entry.get("description", ""),
                    predecessor_node=[action_node_label],
                    edge_type="produces",
                    path=str(path_val),
                )
                created.append(tbl_label)
                table_id_to_label[entry["label"]] = tbl_label
                if str(path_val) != entry["label"]:
                    table_id_to_label[str(path_val)] = tbl_label
                    try:
                        content_paths.add(str(Path(str(path_val)).resolve()))
                    except (OSError, ValueError) as e:
                        logger.debug(f"IR converter: could not resolve path '{path_val}': {e}")
                logger.debug(f"IR converter: created TableNode '{tbl_label}' from structured IR")
            except Exception as e:
                logger.warning(f"IR converter: failed to create TableNode from structured IR: {e}")

        # 2) ColumnNode
        for entry in source.get("column") or []:
            if not isinstance(entry, dict) or "label" not in entry:
                continue
            from_table = entry.get("from_table")
            if not from_table:
                continue
            table_node_label = table_id_to_label.get(from_table)
            if not table_node_label:
                logger.debug(f"IR converter: skip Column '{entry['label']}': no Table for from_table={from_table}")
                continue
            supp = entry.get("supplementary_schemas")
            supp = supp if isinstance(supp, dict) else {}
            vals = entry.get("values")
            if vals is not None and not isinstance(vals, dict):
                vals = {"sample": vals} if isinstance(vals, (list, tuple)) else {"raw": vals}
            lbl = cls._register_column_node(
                context,
                table_node_label,
                label=entry["label"],
                from_table=from_table,
                values=vals,
                supplementary_schemas=supp,
                description=entry.get("description"),
            )
            if lbl:
                created.append(lbl)

        # 3) ToolNode
        for entry in source.get("tool") or []:
            if not isinstance(entry, dict) or "label" not in entry:
                continue

            tool_params = entry.get("tool_params")
            tool_returns = entry.get("tool_returns")
            try:
                label = context.register_node(
                    node_type="Tool",
                    label=entry["label"],
                    description=entry.get("description", ""),
                    predecessor_node=[action_node_label],
                    edge_type="produces",
                    tool_params="" if tool_params is None else str(tool_params),
                    tool_returns="" if tool_returns is None else str(tool_returns),
                )
                created.append(label)
                logger.debug(f"IR converter: created ToolNode '{label}' from structured IR")
            except Exception as e:
                logger.warning(f"IR converter: failed to create ToolNode from structured IR: {e}")

        return created

    @classmethod
    def _create_file_fallback(
        cls,
        context: Context,
        result: Any,
        action_node_label: str,
        tool_name: str,
        workspace: Path | None,
        knowledge_min_length: int = DEFAULT_IR_KNOWLEDGE_MIN_LENGTH,
        visible_result: Any = _VISIBLE_RESULT_UNSET,
    ) -> list[str]:
        """长文本兜底：将工具结果落盘到 workspace 文件，创建 FileNode。

        阈值检测只用模型实际可见的文本（ToolMessage.content 成功分支）：
        Executor 传入的 visible_result 优先，否则按 _visible_result_to_text 回退。
        不让 frontend_msg / data 参与阈值检测。
        """
        text = _visible_result_to_text(result, visible_result)
        if len(text) < knowledge_min_length:
            return []

        if workspace is None:
            logger.debug(f"IR converter: no workspace available for {tool_name}, skip file fallback")
            return []

        try:
            config = getattr(getattr(context, "state", None), "config", None)
            filepath = write_result_to_workspace(text, tool_name, workspace, config=config)
        except Exception as e:
            logger.warning(f"IR converter: failed to write result to workspace for {tool_name}: {e}")
            return []

        label = cls._register_file_node(
            context=context,
            action_node_label=action_node_label,
            tool_name=tool_name,
            path=str(filepath),
        )
        return [label] if label else []

    # ── 节点注册（通用） ─────────────────────────────────────────

    @classmethod
    def _register_table_node(
        cls,
        context: Context,
        action_node_label: str,
        tool_name: str,
        table_path: str,
    ) -> str | None:
        """创建并注册单个 TableNode，返回 graph label；失败返回 None。"""
        try:
            label = context.register_node(
                node_type="Table",
                description="",
                predecessor_node=[action_node_label],
                edge_type="produces",
                path=str(table_path),
            )
            logger.debug(f"IR converter: created TableNode '{label}' from {tool_name} (path={table_path})")
            return label
        except Exception as e:
            logger.warning(f"IR converter: failed to create TableNode for {tool_name}: {e}")
            return None

    @classmethod
    def _register_script_node(
        cls,
        context: Context,
        action_node_label: str,
        tool_name: str,
        *,
        script_content: str,
        script_type: str,
        path: str | None = None,
    ) -> str | None:
        """创建并注册单个 ScriptNode，返回 graph label；失败返回 None。"""
        try:
            label = context.register_node(
                node_type="Script",
                description="",
                predecessor_node=[action_node_label],
                edge_type="produces",
                script_content=script_content,
                script_type=script_type,
                path=path,
                related_data_list=[],
            )
            logger.debug(f"IR converter: created ScriptNode '{label}' from {tool_name}")
            return label
        except Exception as e:
            logger.warning(f"IR converter: failed to create ScriptNode for {tool_name}: {e}")
            return None

    @classmethod
    def _register_file_node(
        cls,
        context: Context,
        action_node_label: str,
        tool_name: str,
        *,
        path: str,
    ) -> str | None:
        """创建并注册单个 FileNode，返回 graph label；失败返回 None。"""
        try:
            label = context.register_node(
                node_type="File",
                description="",
                predecessor_node=[action_node_label],
                edge_type="produces",
                path=path,
                source=tool_name,
            )
            logger.debug(f"IR converter: created FileNode '{label}' from {tool_name}")
            return label
        except Exception as e:
            logger.warning(f"IR converter: failed to create FileNode for {tool_name}: {e}")
            return None

    @classmethod
    def _register_column_node(
        cls,
        context: Context,
        table_node_label: str,
        *,
        label: str,
        from_table: str,
        values: dict[str, Any] | None = None,
        supplementary_schemas: dict[str, Any] | None = None,
        description: str | None = None,
    ) -> str | None:
        """创建并注册单个 ColumnNode，挂载到 TableNode 下；失败返回 None。"""
        supp = supplementary_schemas if isinstance(supplementary_schemas, dict) else {}
        try:
            node_label = context.register_node(
                node_type="Column",
                description=description or "",
                predecessor_node=[table_node_label],
                edge_type="has_column",
                label=label,
                from_table=from_table,
                values=values,
                supplementary_schemas=supp,
            )
            logger.debug(f"IR converter: created ColumnNode '{node_label}' under {table_node_label}")
            return node_label
        except Exception as e:
            logger.warning(f"IR converter: failed to create ColumnNode '{label}': {e}")
            return None

    @classmethod
    def _create_columns_for_table(
        cls,
        context: Context,
        table_node_label: str,
        from_table: str,
        columns: Any,
        data: list[dict[str, Any]],
    ) -> list[str]:
        """为 Table 创建挂载于其下的 ColumnNode。"""
        if columns is None or isinstance(columns, str):
            return []
        col_names = list(columns)
        if not col_names:
            return []
        created: list[str] = []
        for col in col_names:
            col_label = f"{from_table}.{col}" if from_table else col
            values = None
            if data:
                samples = []
                for row in data[:DEFAULT_IR_COLUMN_SAMPLE_ROWS]:
                    if col in row:
                        v = row[col]
                        if v not in samples:
                            samples.append(v)
                        if len(samples) >= DEFAULT_IR_COLUMN_UNIQUE_SAMPLES:
                            break
                if samples:
                    values = {"sample": samples}
            lbl = cls._register_column_node(
                context,
                table_node_label,
                label=col_label,
                from_table=from_table,
                values=values,
                supplementary_schemas={},
            )
            if lbl:
                created.append(lbl)
        return created
