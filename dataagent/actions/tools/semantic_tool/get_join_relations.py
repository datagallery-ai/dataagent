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
from typing import Any

import requests
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.auth import get_metavisor_auth


def _fmt(original: str, frontend: str, data: Any) -> dict:
    return {"original_msg": original, "frontend_msg": frontend, "data": data}


def get_join_relations(table_names: list[str], *, _tool_context: ToolExecutionContext) -> dict:
    """Find joinable relationships among a set of tables.

    Use this tool to discover foreign-key or joinable-column relationships
    between tables, which is critical for writing correct JOIN clauses.

    Args:
        table_names: List of fully-qualified table names, e.g.
            ``["mydb.orders", "mydb.users"]``.

    Returns:
        dict with ``data.joins`` - a list of join-relationship dicts
        containing ``src``, ``target``, and ``evidence``.
    """
    if not table_names:
        return _fmt("未提供表名列表。", "未提供表名列表。", {"joins": []})

    # 过滤空值和空白字符串
    names = [n.strip() for n in table_names if n and n.strip()]
    if not names:
        return _fmt("表名列表为空。", "表名列表为空。", {"joins": []})

    # MetaVisor 服务地址和认证配置
    base_url = _tool_context.config_manager.get("METAVISOR.metavisor_url")
    auth = get_metavisor_auth(_tool_context.config_manager)

    # 构建请求参数
    params = [("dbTableNames", name) for name in names]
    params.append(("limit", 2000))
    url = f"{base_url}/api/metaVisor/v3/advanced-search/joinable-tables"

    try:
        resp = requests.get(url, params=params, auth=auth, timeout=30, headers={"Accept": "application/json"})
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as e:
        logger.error(f"请求 MetaVisor joinable-tables 失败：{e}")
        return _fmt(f"请求失败：{e}", "查询 JOIN 关系失败。", {"joins": []})

    joins: list[dict[str, Any]] = []
    for item in raw:
        src = item.get("src", "")
        targets = item.get("target_column", [])
        evidence = item.get("rel_evidence", "")
        expression = item.get("expression", "")
        rel_type = item.get("rel_type", "")
        for tgt in targets:
            joins.append(
                {
                    "src": src,
                    "target": tgt,
                    "evidence": evidence,
                    "expression": expression,
                    "rel_type": rel_type,
                }
            )

    tbl_str = ", ".join(names)
    summary = f"查询表：[{tbl_str}]  →  找到 {len(joins)} 条 JOIN 关系。"
    lines = [
        f"  - {j['src']} → {j['target']}"
        + (f"  [{j['rel_type']}]" if j["rel_type"] else "")
        + (f"  ({j['evidence']})" if j["evidence"] else "")
        + (f"  {j['expression']}" if j["expression"] else "")
        for j in joins
    ]
    detail = summary + "\n" + "\n".join(lines) if lines else summary

    preview_lines: list[str] = [summary]
    if joins:
        preview_lines.append("JOIN 关系 (前5):" if len(joins) > 5 else "JOIN 关系:")
        for j in joins[:5]:
            line = f"  - {j['src']} → {j['target']}"
            if j["rel_type"]:
                line += f"  [{j['rel_type']}]"
            if j["evidence"]:
                line += f"  ({j['evidence']})"
            if j["expression"]:
                line += f"\n    条件：{j['expression']}"
            preview_lines.append(line)
        if len(joins) > 5:
            preview_lines.append(f"  … 还有 {len(joins) - 5} 条关系")
    msg = "\n".join(preview_lines)
    return _fmt(detail, msg, {"joins": joins})
