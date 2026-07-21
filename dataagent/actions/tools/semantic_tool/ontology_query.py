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
"""Ontology 工具(新版 SemanticService API 适配)。

通过 :class:`SemanticServiceClient` 调用语义服务的**基础检索 REST 接口**查询本体数据:

- 实体:``get_table_list``(``advanced-search/table-list``);
- 属性:``get_table_columns_info``(``advanced-search/table-columns-info``);
- 关系:``get_joinable_tables``(``advanced-search/joinable-tables``)。

场景(databaseName)统一取自 ``DATABASE.db_id``,与其它 semantic 工具同源。
渲染出的文本格式与老 ``_load_ontology_fixture`` 一致(三段式 + 相同字段名),
保证模型 prompt 形态稳定。
"""

from __future__ import annotations

import json
from typing import Any

import requests
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.actions.tools.semantic_tool.semantic_client import SemanticServiceClient
from dataagent.utils.constants import DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT

# Ontology 渲染需要拿到场景下的表/列,基础检索接口默认 25 上限过小,这里放大;
# 但上限有意保守,避免超大场景把逐表列查询(N+1)与 prompt 撑爆。
_ONTOLOGY_TABLE_LIST_LIMIT = 200
_ONTOLOGY_TABLE_COLUMNS_LIMIT = 1000
# joinable-tables 用重复 GET 参数传表名,全量表名一次性传会撑爆 URL(414),分批发送。
_JOINABLE_TABLES_BATCH = 50


def _resolve_databases(ctx: ToolExecutionContext) -> list[str]:
    """Resolve the semantic-service database name(s) from ``DATABASE.db_id``.

    The ontology scene is the semantic-service ``databaseName``. It is read from
    the same ``DATABASE.db_id`` config the other semantic tools use (single
    string or list).
    """
    raw = ctx.config_manager.get("DATABASE.db_id", "")
    if isinstance(raw, list):
        dbs = [str(s).strip() for s in raw if s and str(s).strip()]
    elif isinstance(raw, str) and raw.strip():
        dbs = [raw.strip()]
    else:
        dbs = []
    if not dbs:
        raise ValueError("Ontology database is required: set DATABASE.db_id in config.")
    return dbs


def _client(ctx: ToolExecutionContext) -> SemanticServiceClient:
    return SemanticServiceClient.from_config(ctx.config_manager)


def _table_key(qualified_name: str) -> str:
    """Reduce a possibly column-qualified name to its owning ``db.table``.

    Business table names are always two dotted segments (``db.table``) and the
    joinable columns are always three (``db.table.col``), so a plain two-segment
    heuristic is sufficient; dangling filtering happens separately in
    :func:`_fetch_relations`.
    """
    name = str(qualified_name or "")
    parts = name.split(".")
    return ".".join(parts[:2]) if len(parts) >= 3 else name


def _fetch_entities(client: SemanticServiceClient, database: str) -> list[dict[str, Any]]:
    """List business tables (entities) of one database via ``get_table_list``.

    ``get_table_list`` returns a list of single-key dicts keyed by the
    fully-qualified table name (``db.table``); the value carries the table
    description. Normalized to ``[{table_name, table_description}]``.
    """
    table_items = client.get_table_list(database, limit=_ONTOLOGY_TABLE_LIST_LIMIT)

    entities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in table_items or []:
        if not isinstance(item, dict):
            continue
        for name, meta in item.items():
            if not name or name in seen:
                continue
            seen.add(name)
            meta = meta if isinstance(meta, dict) else {}
            description = meta.get("table_description_enhanced") or meta.get("table_description", "")
            entities.append({"table_name": name, "table_description": description})
    if len(entities) >= _ONTOLOGY_TABLE_LIST_LIMIT:
        logger.warning(
            "ontology: database {} hit table-list limit ({}); ontology may be truncated.",
            database,
            _ONTOLOGY_TABLE_LIST_LIMIT,
        )
    entities.sort(key=lambda e: e["table_name"])
    return entities


def _fetch_columns(client: SemanticServiceClient, table_name: str) -> list[dict[str, Any]]:
    """List columns of one business table via ``get_table_columns_info``.

    ``get_table_columns_info`` returns ``{"db.table.col": {column meta}}``.
    Normalized to ``[{column_name, column_description}]``. A single failing
    table is logged and skipped (returns ``[]``) so one bad table does not abort
    the whole ontology load.
    """
    try:
        cols_raw = client.get_table_columns_info(table_name, limit=_ONTOLOGY_TABLE_COLUMNS_LIMIT)
    except (requests.RequestException, ValueError) as err:
        logger.warning("ontology: failed to load columns for {}: {}", table_name, err)
        return []
    if not isinstance(cols_raw, dict):
        return []

    columns: list[dict[str, Any]] = []
    for dtc, meta in cols_raw.items():
        meta = meta if isinstance(meta, dict) else {}
        column_name = dtc.split(".")[-1] if isinstance(dtc, str) else str(dtc)
        columns.append(
            {
                "column_name": column_name,
                "column_description": meta.get("column_short_description", ""),
            }
        )
    columns.sort(key=lambda c: c["column_name"])
    return columns


def _fetch_relations(
    client: SemanticServiceClient,
    table_names: list[str],
) -> list[dict[str, Any]]:
    """Discover table-level join relationships via ``get_joinable_tables``.

    ``get_joinable_tables`` returns column-level join candidates
    (``src`` / ``target_column`` / ``expression`` / ``rel_type``). Requests are
    batched (repeated ``dbTableNames`` params would otherwise blow the URL length
    for large scenes). Candidates are reduced to table-level triplets, filtered
    to relations whose both ends are known scene tables, and aggregated per
    ``(source, target)`` so multiple join columns are all preserved.
    """
    if not table_names:
        return []

    known = set(table_names)
    raw: list[Any] = []
    for start in range(0, len(table_names), _JOINABLE_TABLES_BATCH):
        batch = table_names[start : start + _JOINABLE_TABLES_BATCH]
        resp = client.get_joinable_tables(batch, limit=DEFAULT_SEMANTIC_SERVICE_JOINABLE_TABLES_LIMIT)
        raw.extend(resp or [])

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        source_table = _table_key(item.get("src", ""))
        expression = str(item.get("expression", "")).strip()
        cardinality = item.get("rel_type", "") or ""
        for tgt in item.get("target_column", []) or []:
            target_table = _table_key(tgt)
            if not source_table or not target_table or source_table == target_table:
                continue
            # Drop dangling relations pointing outside the scene's entity set.
            if source_table not in known or target_table not in known:
                continue
            entry = agg.setdefault((source_table, target_table), {"cardinality": cardinality, "conditions": []})
            if expression and expression not in entry["conditions"]:
                entry["conditions"].append(expression)

    relations = [
        {
            "source_table": source_table,
            "target_table": target_table,
            "join_condition": "；".join(entry["conditions"]),
            "cardinality": entry["cardinality"],
        }
        for (source_table, target_table), entry in agg.items()
    ]
    relations.sort(key=lambda r: (r["source_table"], r["target_table"]))
    return relations


def _strip_db_prefix(name: str, db_prefixes: list[str]) -> str:
    """Strip a leading, known ``<db>.`` prefix from an entity/table name.

    Only strips a prefix that matches one of the *configured* databases, so a
    table name that itself contains dots is left intact when no known prefix
    applies. ``db_prefixes`` is deliberately empty in ambiguous scenes (see
    :func:`_render_ontology_description`) to avoid producing colliding names.
    """
    text = str(name or "")
    for db in db_prefixes:
        prefix = f"{db}."
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _render_ontology_description(
    entities: list[dict[str, Any]],
    columns_by_table: dict[str, list[dict[str, Any]]],
    relations: list[dict[str, Any]],
    scene: str,
    databases: list[str] | None = None,
) -> dict[str, str]:
    """Render the three-section ontology text consumed by the main Agent.

    Format mirrors the legacy ``_load_ontology_fixture`` output so the model
    prompt stays byte-stable: ``object_types`` list, ``object_type_details``
    with ``entity_name``/``entity_description``/``properties``, and
    ``relation_triplets`` with ``source``/``relation``/``target``/
    ``cardinality``/``description``.

    Entity/relation names are rendered *without* their ``<db>.`` prefix so the
    prompt shows bare table names (e.g. ``antibodies`` rather than
    ``changping02.antibodies``). Internal fetch logic keeps using the fully
    qualified ``db.table`` keys; stripping happens only here in the render
    layer. To avoid ambiguity, prefixes are only stripped when exactly one
    database is configured; multi-database scenes keep the qualifier so two
    same-named tables from different databases stay distinguishable.
    """
    dbs = databases or []
    db_prefixes = dbs if len(dbs) == 1 else []

    def _display(name: str) -> str:
        return _strip_db_prefix(name, db_prefixes)

    object_types = [_display(e.get("table_name", "")) for e in entities]

    object_type_details: list[dict[str, Any]] = []
    for e in entities:
        name = e.get("table_name", "")
        cols = columns_by_table.get(name, [])
        props = [
            {
                "property_name": c.get("column_name", ""),
                "property_description": c.get("column_description", ""),
            }
            for c in cols
        ]
        object_type_details.append(
            {
                "entity_name": _display(name),
                "entity_description": e.get("table_description", ""),
                "properties": props,
            }
        )

    relation_triplets: list[dict[str, Any]] = []
    for r in relations:
        source = _display(r.get("source_table", ""))
        target = _display(r.get("target_table", ""))
        relation_triplets.append(
            {
                "source": source,
                "relation": f"{source}关联到{target}" if source and target else "",
                "target": target,
                "cardinality": r.get("cardinality") or "",
                "description": r.get("join_condition") or "",
            }
        )

    def _pretty(obj: list[dict]) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    return {
        "original_msg": (
            f"\n对本体查询结果如下：\n"
            f"本体目前包含以下几种类型实体：\n{_pretty(object_types)}\n\n"
            f"每种实体的描述和属性定义如下:\n{_pretty(object_type_details)}\n\n"
            f"实体之间有以下几种类型的关联，每种关联用(源实体-关系-目标实体)的三元组表示:\n"
            f"{_pretty(relation_triplets)}\n\n"
            f"可以根据以上信息理解实体间的关联关系，以及每个实体的属性含义，从而构造查询条件。\n"
        ),
        "frontend_msg": (
            f"已从语义层服务加载本体 {scene} 描述信息，本体中共包括"
            f"{len(object_types)}种实体，{len(relation_triplets)}种关系，"
            f"它们的具体schema也已经被加载。"
        ),
    }


def get_ontology_description(*, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """Fetch the entity and relation structure description for the current scene.

    Uses the semantic-service basic retrieval REST APIs (``get_table_list`` /
    ``get_table_columns_info`` / ``get_joinable_tables``) to load ontology
    metadata and renders it into the text consumed by the main Agent. Call
    this tool AT MOST ONCE and FIRST.

    Use when:
    - You need to understand the overall ontology structure, entity types, or relation definitions.

    Returns:
        A dict containing ontology metadata — entity types, attributes, and relations.
        If the semantic service is unreachable, degrades to an empty ontology
        with a failure notice rather than aborting the whole agent turn.
    """
    databases = _resolve_databases(_tool_context)
    client = _client(_tool_context)
    scene = ", ".join(databases)

    try:
        entities: list[dict[str, Any]] = []
        for db in databases:
            entities.extend(_fetch_entities(client, db))

        columns_by_table: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            table_name = e.get("table_name", "")
            if table_name and table_name not in columns_by_table:
                columns_by_table[table_name] = _fetch_columns(client, table_name)

        relations = _fetch_relations(client, [e["table_name"] for e in entities if e.get("table_name")])
    except (requests.RequestException, ValueError) as err:
        logger.error(f"加载本体描述失败：{err}")
        rendered = _render_ontology_description([], {}, [], scene, databases)
        rendered["frontend_msg"] = f"本体 {scene} 加载失败：{err}"
        return rendered

    return _render_ontology_description(entities, columns_by_table, relations, scene, databases)
