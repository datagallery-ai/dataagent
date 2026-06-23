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
import copy
from itertools import product
from typing import Any

import requests
from dataagent.common_utils.knowledge_base.memory import MemoryFactory
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate
from loguru import logger

from dataagent.actions.tools.context import ToolExecutionContext


class MetaVisorClient:
    """Client to interact with MetaVisor API."""

    def __init__(self, metavisor_url: str):
        """
        Initialize the MetaVisor client.
        """
        self.base_url = f"http://{metavisor_url}/api/metaVisor/v3/advanced-search/"
        self.s = requests.Session()
        self.headers = {"Accept": "application/json"}

    def get_table_list(self, db: str) -> list:
        """
        Get the list of tables in the specified database.
        """
        return self._get("table-list", params={"databaseName": db})

    def get_table_columns_info(self, table_name: str) -> list:
        """
        Get information about the columns of a specified table.
        """
        return self._get("table-columns-info", params={"tableName": table_name})

    def get_columns_value_info(self, column_names: list) -> dict:
        """
        Get value information for specified columns.
        """
        return self._get("column-value-info", params={"columnNames": column_names})

    def get_joinable_tables(self, table_names: list[str]) -> list:
        """
        Get joinable tables for the specified table names.
        """
        params = [("dbTableNames", t) for t in table_names]
        return self._get("joinable-tables", params=params)  # type: ignore

    def semantic_search_column(self, db: str, keywords: list[str], top_k: int) -> dict:
        """
        Perform semantic search on columns based on keywords.
        """
        return self._get(
            "semantic-search-columns",
            params={
                "databaseName": db,
                "keywords": keywords,
                "topK": top_k,
                "searchColumns": "true",
                "searchValues": "false",
            },
        )

    def get_sql_few_shots(self, semantic_query: str) -> list[str]:
        """
        Get SQL few-shot examples based on a semantic query. Not implemented.
        """
        raise NotImplementedError("SQL few shots not implemented.")

    def _get(self, path: str, params: dict[str, Any] | None = None):
        """
        Internal method to perform GET requests.
        """
        url = f"{self.base_url}{path}"
        resp = self.s.get(url, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()


def extract_keywords(query: str) -> dict[str, str | dict[str, set[str]]]:
    """
    Extract keywords of different categories from user query, including Tools, Workflows, Names, Annotations, Entities.
    All the extracted keywords can be used as search queries of different information source, regardless of their
    categories.

    Args:
        query (str): user query

    Returns:
        dict[str, str | dict[str, set[str]]], default agent tool output, including three keys:
            - original_msg: tool output to be given to agent llm model
            - frontend_msg: tool output to be displayed at the frontend
            - data: extracted keywords in a dict, with keys being category names
    """
    context = {"query": query, "knowledge": ""}
    llm = llm_manager.get_default_llm()
    system_prompt = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/perceptor/keyword_extract_system"
    ).apply_prompt_template(**context)
    user_prompt = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/perceptor/keyword_extract_user"
    ).apply_prompt_template(**context)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    res = llm.invoke(messages).content.strip()
    dict_result: dict[str, set[str]] = {}
    output_message: str = "已分解用户query中的关键词，其中按类型包括："
    for line in res.split("\n"):
        if ": " not in line or " - " not in line:
            continue

        type_, keyword = line.split(": ")
        if type_ not in dict_result:
            dict_result[type_] = set()

        split_keywords = keyword.split(" - ")
        if len(split_keywords) != 2:
            continue

        dict_result[type_] = dict_result[type_].union(set(split_keywords[1].strip().split("|")))

    for i, j in dict_result.items():
        output_message += f"\n{i}相关关键词：{str(j)}"

    return {
        "original_msg": output_message,
        "frontend_msg": output_message,
        "data": dict_result,
    }


def perceive_metadata_from_metavisor(
    keywords: list[str],
    top_k: int = 5,
    *,
    _tool_context: ToolExecutionContext,
) -> dict:
    """
    Perceive metadata from MetaVisor based on given keywords.

    Args:
        keywords (list[str]): List of keywords to search for.
        top_k (int): Number of top results to retrieve.

    Returns:
        dict: A dictionary containing the perceived metadata information.
    """
    if not keywords:
        return {
            "original_msg": "未提供关键词，跳过从 MetaVisor 感知元数据步骤。",
            "frontend_msg": "未提供关键词，跳过从 MetaVisor 感知元数据步骤。",
            "data": {},
        }
    # metavisor 配置, 之后需要在配置文件中添加相关配置项
    cm = _tool_context.config_manager
    host = cm.get("METAVISOR.host", "localhost")
    port = cm.get("METAVISOR.port", 31000)
    db = cm.get("DATABASE.db_id", "CP")

    client = MetaVisorClient(f"{host}:{port}")
    dict_result = {"table": [], "column": []}

    table_set = set()
    column_set = set()

    tables_info_mapping = {}
    for item in client.get_table_list(db):
        tables_info_mapping.update(item)

    for item in client.semantic_search_column(db, list(keywords), top_k):
        payload = next(iter(item.values()))
        for entry in payload["column_name_search"]:
            d, t, c = next(iter(entry)).split(".")
            table_desc = ""
            table_info = tables_info_mapping.get(f"{d}.{t}")
            if table_info:
                table_desc = (
                    table_info.get("table_description_enhanced")
                    if table_info.get("table_description_enhanced", "")
                    else table_info.get("table_description", "")
                )
            if f"{d}.{t}" not in table_set:
                table_set.add(f"{d}.{t}")
                dict_result["table"].append(
                    {
                        "label": f"{d}.{t}",
                        "description": table_desc,
                        "path": f"{d}.{t}",
                    }
                )
            column_set.add(f"{d}.{t}.{c}")

    joinable_mapping = {}
    tables = list(table_set)
    columns = list(column_set)
    for item in client.get_joinable_tables(tables):
        from_table = item.get("src", "")
        to_table = item.get("target_column", [])
        if from_table and to_table:
            joinable_mapping[from_table] = {
                "label": to_table[0],
                "type": "is_joinable_with",
                "evidence": item.get("rel_evidence", ""),
            }
            joinable_mapping[to_table[0]] = {
                "label": from_table,
                "type": "is_joinable_with",
                "evidence": item.get("rel_evidence", ""),
            }

    columns_info = client.get_columns_value_info(columns)
    for col in columns:
        info = columns_info.get(col, {})
        supplementary_schemas = []
        if joinable_mapping.get(col, {}):
            supplementary_schemas.append(joinable_mapping.get(col, {}))
        dict_result["column"].append(
            {
                "label": col,
                "description": info.get("column_description", ""),
                "from_table": col.rsplit(".", 1)[0],
                "values": info.get("sampled_values", []),
                "supplementary_schemas": supplementary_schemas,
            }
        )
    output_message = f"Found {len(dict_result['table'])} tables and {len(dict_result['column'])} columns."

    return {
        "original_msg": output_message + "\n" + str(dict_result),
        "frontend_msg": output_message,
        "data": dict_result,
    }


def perceive_knowledge_from_ontology():
    """To be implemented."""
    raise NotImplementedError("To be implemented.")


def perceive_knowledge_from_memory(
    keywords_list: list[str],
    topk: int = 3,
    rag_id: str | None = None,
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, Any]:
    """
    Perceive knowledge nodes from memory backend using hybrid retrieval and graph expansion.

    Args:
        keywords_list (list[str]): List of keywords used to retrieve relevant knowledge nodes.
        topk (int): Number of top results to retrieve per query (for each schema/mode combination).
        rag_id (str): Rag identifier.

    Returns:
        dict[str, Any]: Default agent tool output, including:
            - original_msg: tool output to be given to agent llm model
            - frontend_msg: tool output to be displayed at the frontend
            - data: retrieved knowledge nodes in the format of knowledge IR
    """
    if not rag_id:
        rag_id = _tool_context.config_manager.get("MEMORY.index_prefix", "ecommerce")
    mem = MemoryFactory.get_memory(rag_id=rag_id, config_manager=_tool_context.config_manager)
    retrieved_nodes: dict[int, dict[str, Any]] = {}

    schemas = ("label", "description")
    modes = ("vector", "fulltext")
    for keyword, schema, mode in product(keywords_list, schemas, modes):
        for candidate in mem.query_knowledge(
            "graph_node", query_schema=schema, query_text=keyword, mode=mode, topk=topk
        ):
            node_content = candidate["doc_content"]
            node_id = int(node_content["id"])
            if node_id not in retrieved_nodes:
                retrieved_nodes[node_id] = dict(node_content)

    start_ids = set(retrieved_nodes.keys())
    if start_ids:
        graph_nodes, _ = mem.query_graph("G2", type_="bfs", sources=start_ids, depth=3)
        for node_id, node in graph_nodes.items():
            if node_id not in retrieved_nodes:
                retrieved_nodes[node_id] = dict(node)

    calc_keywords = ("计算", "公式", "sql", "metric", "指标", "聚合", "sum", "avg", "count")
    for node in retrieved_nodes.values():
        if node.get("knowledge_type"):
            continue
        text = f"{node.get('label', '')} {node.get('description', '')} {node.get('knowledge_content', '')}".lower()
        node["knowledge_type"] = "calculation" if any(k in text for k in calc_keywords) else "domain"

    knowledge_items: list[dict[str, Any]] = []
    for node in retrieved_nodes.values():
        label = node.get("label") or ""
        if not label:
            continue
        knowledge_items.append(
            {
                "label": label,
                "description": "",
                "knowledge_type": node.get("knowledge_type", ""),
                "knowledge_content": node.get("description", ""),
            }
        )

    retrieved_text: dict[int, dict[str, Any]] = {}
    for keyword, mode in product(keywords_list, modes):
        for candidate in mem.query_knowledge("text", query_schema="info", query_text=keyword, mode=mode, topk=topk):
            node_content = candidate["doc_content"]
            node_id = candidate["doc_id"]
            if node_id not in retrieved_nodes:
                retrieved_text[node_id] = dict(node_content)

    for text in retrieved_text.values():
        knowledge_items.append(
            {
                "label": None,
                "description": "",
                "knowledge_type": "domain",
                "knowledge_content": text.get("info", ""),
            }
        )

    return {
        "original_msg": f"Found {len(knowledge_items)} knowledge node(s)." + "\n" + str({"knowledge": knowledge_items}),
        "frontend_msg": f"Found {len(knowledge_items)} knowledge node(s).",
        "data": {"knowledge": knowledge_items},
    }


def _build_metadata_column_output(
    label: str,
    description: str,
    from_table: str,
    supplementary_schemas: list,
) -> dict[str, Any]:
    """Build a column IR dict for metadata search results.

    Args:
        label: Column label (often ``table.column``).
        description: Column description text.
        from_table: Parent table label.
        supplementary_schemas: Normalized relationship metadata list.

    Returns:
        Column IR dict with ``values`` set to ``None`` (typical values not yet supported).
    """
    return {
        "label": label,
        "description": description,
        "from_table": from_table,
        "values": None,  # 需要Memory适配典型值
        "supplementary_schemas": supplementary_schemas,
    }


def _normalize_supplementary_schemas(relationship: list) -> list:
    """Deep-copy and normalize relationship labels for metadata column output.

    Args:
        relationship: Raw relationship list from memory metadata.

    Returns:
        Copied list with shortened labels and empty ``evidence`` fields.
    """
    temp = copy.deepcopy(relationship)
    for relation in temp:
        relation["label"] = ".".join(relation["label"].split("/")[-2:]).replace(" -> ", ".")
        relation["evidence"] = ""
    return temp


def _process_metadata_file_hit(
    mem: Any,
    hit: dict[str, Any],
    retrieved_table: set[int],
    retrieved_column: set[int],
    output_table: list[dict[str, str]],
    output_column: list[dict[str, Any]],
) -> None:
    """Append table/column IR entries when a file-type metadata hit is retrieved.

    Mutates ``retrieved_*`` sets and ``output_*`` lists in place (same semantics as inline loop body).

    Args:
        mem: Memory backend instance.
        hit: Single query result item from ``mem.query_table``.
        retrieved_table: Dedup set of retrieved table ids.
        retrieved_column: Dedup set of retrieved column ids.
        output_table: Accumulated table IR list.
        output_column: Accumulated column IR list.
    """
    content = hit["metadata_content"]
    if content["type"] != "file" or content["id"] in retrieved_table:
        return

    retrieved_table.add(content["id"])
    output_table.append(
        {
            "label": ".".join(content["label"].split("/")[-2:]),
            "description": content["description"],
            "path": content["label"],
        }
    )
    for key, val in mem.metadata.metadata[content["path"]]["schema"].items():
        if val["id"] in retrieved_column:
            continue
        retrieved_column.add(val["id"])
        temp = _normalize_supplementary_schemas(val["relationship"])
        output_column.append(
            _build_metadata_column_output(
                label=".".join(content["path"].split("/")[-2:]) + "." + key,
                description=val["schema_description"],
                from_table=".".join(content["path"].split("/")[-2:]),
                supplementary_schemas=temp,
            )
        )


def _process_metadata_column_hit(
    mem: Any,
    hit: dict[str, Any],
    retrieved_table: set[int],
    retrieved_column: set[int],
    output_table: list[dict[str, str]],
    output_column: list[dict[str, Any]],
) -> None:
    """Append table/column IR entries when a column-type metadata hit is retrieved.

    Mutates ``retrieved_*`` sets and ``output_*`` lists in place (same semantics as inline loop body).

    Args:
        mem: Memory backend instance.
        hit: Single query result item from ``mem.query_table``.
        retrieved_table: Dedup set of retrieved table ids.
        retrieved_column: Dedup set of retrieved column ids.
        output_table: Accumulated table IR list.
        output_column: Accumulated column IR list.
    """
    content = hit["metadata_content"]
    if content["type"] != "column" or content["id"] in retrieved_column:
        return

    retrieved_column.add(content["id"])
    temp = _normalize_supplementary_schemas(content["relationship"])
    output_column.append(
        _build_metadata_column_output(
            label=".".join(content["path"].split("/")[-2:]) + "." + content["label"],
            description=content["description"],
            from_table=".".join(content["path"].split("/")[-2:]),
            supplementary_schemas=temp,
        )
    )
    if mem.metadata.metadata[content["path"]]["id"] not in retrieved_table:
        retrieved_table.add(mem.metadata.metadata[content["path"]]["id"])
        output_table.append(
            {
                "label": ".".join(content["path"].split("/")[-2:]),
                "description": mem.metadata.metadata[content["path"]]["file_description"],
                "path": content["path"],
            }
        )

    for j in content["relationship"]:
        split = j["label"].split(" -> ")
        if mem.metadata.metadata[split[0]]["schema"][split[1]]["id"] not in retrieved_column:
            retrieved_column.add(mem.metadata.metadata[split[0]]["schema"][split[1]]["id"])

            temp = _normalize_supplementary_schemas(mem.metadata.metadata[split[0]]["schema"][split[1]]["relationship"])
            output_column.append(
                _build_metadata_column_output(
                    label=".".join(j["label"].split("/")[-2:]).replace(" -> ", "."),
                    description=mem.metadata.metadata[split[0]]["schema"][split[1]]["schema_description"],
                    from_table=".".join(split[0].split("/")[-2:]),
                    supplementary_schemas=temp,
                )
            )

        if mem.metadata.metadata[split[0]]["id"] not in retrieved_table:
            retrieved_table.add(mem.metadata.metadata[split[0]]["id"])
            output_table.append(
                {
                    "label": ".".join(split[0].split("/")[-2:]),
                    "description": mem.metadata.metadata[split[0]]["file_description"],
                    "path": split[0],
                }
            )


def perceive_metadata_from_memory(
    keywords_list: list[str],
    *,
    _tool_context: ToolExecutionContext,
) -> dict[str, str | dict[str, list[dict[str, Any]]]]:
    """
    Perform metadata search from memory backend. This function uses given keywords as query texts to retrieve relevant
    tables and columns. It is guaranteed that if a column is retrieved, the table it belongs to is also retrieved.

    Args:
        keywords_list (list[str]): list of keywords used to extract relevant tables and columns

    Returns:
        dict[str, str | dict[str, list[dict[str, Any]]]], default agent tool output, including three keys:
            - original_msg: tool output to be given to agent llm model
            - frontend_msg: tool output to be displayed at the frontend
            - data: retrieved tables and columns from memory, in the format of table and column IR
    """
    cm = _tool_context.config_manager
    rag_id = cm.get("MEMORY.index_prefix", "ecommerce")
    mem = MemoryFactory.get_memory(rag_id=rag_id, config_manager=cm)
    retrieved_table: set[int] = set()
    retrieved_column: set[int] = set()
    output_table: list[dict[str, str]] = []
    output_column: list[dict[str, Any]] = []

    tables, schemas, modes = ("table", "column"), ("label", "description"), ("fulltext", "vector")
    for keyword, table, schema, mode in product(keywords_list, tables, schemas, modes):
        if (table, schema, mode) == ("table", "label", "vector"):
            continue

        out = mem.query_table(
            type_=table, query_text=keyword, mode=mode, query_schema=schema, topk=3, similarity_threshold=0.3
        )
        for i in out:
            _process_metadata_file_hit(mem, i, retrieved_table, retrieved_column, output_table, output_column)
            _process_metadata_column_hit(mem, i, retrieved_table, retrieved_column, output_table, output_column)

    return {
        "original_msg": f"Found {len(output_table)} table(s) and {len(output_column)} column(s)."
        + "\n"
        + str({"table": output_table, "column": output_column}),
        "frontend_msg": f"Found {len(output_table)} table(s) and {len(output_column)} column(s).",
        "data": {"table": output_table, "column": output_column},
    }


def perceive_data_from_ontology(query: str, *, _tool_context: ToolExecutionContext) -> dict[str, Any]:
    """
    Retrieve data-related ontology content via a third-party ontology service using natural language query.

    Args:
        query (str): The original user natural language query.

    Returns:
        dict[str, Any]: Default agent tool output, including:
            - original_msg: Detailed log of the ontology query result.
            - frontend_msg: Brief summary message for frontend display.
            - data: The raw JSON response content from the ontology service.
    """
    logger.debug("Perceptor(atom): running ontology data search...")

    # 1) Use the original query as nl_query
    nl_query = query

    # 2) Get ontology URL from config_manager
    ontology_url = _tool_context.config_manager.get("ONTOLOGY_SERVICE_URL")
    if not ontology_url:
        logger.warning("ONTOLOGY_SERVICE_URL is not configured.")
        return {
            "original_msg": "Missing configuration: ONTOLOGY_SERVICE_URL",
            "frontend_msg": "Ontology service not configured.",
            "data": {},
        }

    # 3) Call third-party ontology service
    try:
        response = requests.post(
            ontology_url,
            json={"nl_query": nl_query},
            timeout=30,
        )
        response.raise_for_status()
        result_payload = response.json()

        success = result_payload.get("success", False)
        message = result_payload.get("message", "")
        result = result_payload.get("result", {})

        if not success:
            logger.error(f"Ontology service error: {message}")
            return {
                "original_msg": f"Ontology service error: {message}",
                "frontend_msg": "Failed to retrieve data from ontology.",
                "data": {},
            }

        result_payload = result
    except Exception as exc:
        logger.error(f"Ontology request failed: {exc}")
        return {
            "original_msg": f"Ontology request failed: {exc}",
            "frontend_msg": "Failed to retrieve data from ontology.",
            "data": {},
        }

    # 4) Return in Flex Agent format
    return {
        "original_msg": "Ontology query succeeded." + "\n" + str(result_payload),
        "frontend_msg": "Successfully retrieved relevant data from ontology.",
        "data": result_payload,
    }


def perceive_tool_from_actionmanager(tool_manager: Any = None) -> dict[str, str | dict[str, list[dict[str, Any]]]]:
    """
    Get all available tools from action manager.

    Args:
        tool_manager: per-Agent ToolManager instance. If None, a fresh empty instance is created.

    Returns:
        dict[str, str | dict[str, list[dict[str, Any]]]], default agent tool output, including three keys:
            - original_msg: tool output to be given to agent llm model
            - frontend_msg: tool output to be displayed at the frontend
            - data: retrieved tool and skill from memory, in the format of tool and skill IR
    """
    if tool_manager is None:
        from dataagent.core.managers.action_manager.manager import ToolManager

        tool_manager = ToolManager()

    output_tool: list[dict[str, Any]] = []
    output_skill: list[dict[str, Any]] = []

    # 工具信息
    tools = tool_manager.list_tools()
    for name in tools:
        tool_schema = tool_manager.get_tool_info(name)["schema"]
        output_tool.append(
            {
                "label": tool_schema["name"],
                "description": tool_schema["description"],
                "tool_params": tool_schema["parameters"],
                "tool_returns": tool_schema["output"],
            }
        )

    # skill 信息
    skills_meta = getattr(tool_manager, "list_skills", lambda: [])()
    for meta in skills_meta:
        output_skill.append(
            {
                "label": meta.get("name", ""),
                "description": meta.get("description", ""),
                "path": meta.get("path", ""),
                "category": meta.get("category", ""),
                "tags": meta.get("tags", []),
            }
        )

    return {
        "original_msg": f"Found {len(output_tool)} tool(s) and {len(output_skill)} skill(s)."
        + "\n"
        + str({"tool": output_tool, "skill": output_skill}),
        "frontend_msg": f"Found {len(output_tool)} tool(s) and {len(output_skill)} skill(s).",
        "data": {"tool": output_tool, "skill": output_skill},
    }
