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
import json
import os
import threading
import traceback
from typing import Any, NamedTuple

import networkx as nx
import pandas as pd
from loguru import logger

from dataagent.common_utils.knowledge_base.knowledge_base import KnowledgeBase
from dataagent.common_utils.knowledge_base.metadata_management import MetadataManagement
from dataagent.common_utils.knowledge_base.tool_management import ToolManagement
from dataagent.common_utils.knowledge_base.utils_common import MySQLReader
from dataagent.common_utils.knowledge_base.utils_inference import model_inference
from dataagent.common_utils.knowledge_base.utils_memory import graph_to_html, html_config
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate


class NullTool:
    """空实现的工具元数据管理，兼容 ToolManagement 的最小接口。用于 MEMORY 未启用场景。"""

    def __init__(self) -> None:
        """init tool"""
        # 与 ToolManagement.tool 字段保持一致，便于外部直接访问
        self.tool: dict[str, dict[str, Any]] = {}
        self.storage = None  # Add a placeholder for the storage attribute
        self.index_edges = None  # Add a placeholder for the index_edges attribute
        self.index_nodes = None  # Add a placeholder for the index_nodes attribute

    @staticmethod
    def remove_tool(*_, **__) -> None:
        """remove tool"""
        return None

    @staticmethod
    def register_tool(*_, **__) -> None:
        """register tool"""
        return None

    @staticmethod
    def pull_tool_metadata(*_, **__) -> None:
        """pull tool metadata"""
        return None

    @staticmethod
    def query_tool_metadata(*_, **__) -> list[dict]:
        """query tool metadata"""
        return []

    @staticmethod
    def push_tool_metadata(*_, **__) -> None:
        """push tool metadata"""
        return None

    @staticmethod
    def add_tool_edge(*_, **__) -> None:
        """add tool edge"""
        return None


class NullKb:
    """空实现的知识库管理，兼容 KnowledgeBase 的最小接口，用于 MEMORY 未启用场景。"""

    def __init__(self) -> None:
        """Initialize NullKb with a storage attribute."""
        self.storage = None
        self.index_texts = None  # Add a placeholder for the index_texts attribute
        self.index_reports = None  # Add a placeholder for the index_reports attribute
        self.index_nodes = None  # Add a placeholder for the index_nodes attribute
        self.index_edges = None  # Add a placeholder for the index_edges attribute
        self.index_unified_semantics = None  # Add a placeholder for the index_unified_semantics attribute

    @staticmethod
    def insert_graph(*_, **__) -> None:
        """空实现的插入图方法"""
        return None

    @staticmethod
    def process_markdown(*_, **__) -> None:
        """空实现的处理markdown方法"""
        return None

    @staticmethod
    def process_user_query(*_, **__) -> None:
        """空实现的处理用户查询方法"""
        return None

    @staticmethod
    def query_document(*_, **__) -> list[dict]:
        """空实现的查询文档方法"""
        return []

    @staticmethod
    def delete_graph(*_, **__) -> None:
        """空实现的删除图方法"""
        return None

    @staticmethod
    def delete_document(*_, **__) -> None:
        """空实现的删除文档方法"""
        return None


class NullMetadata:
    """空实现的元数据管理，兼容 MetadataManagement 的最小接口，用于 MEMORY 未启用场景。"""

    def __init__(self) -> None:
        """init metadata"""
        self.storage = None  # Add a placeholder for the storage attribute
        self.index_tables = None  # Add a placeholder for the index_tables attribute
        self.metadata = {}  # Add a placeholder for the metadata attribute
        self.index_columns = None  # Add a placeholder for the index_columns attribute
        self.index_nodes = None  # Add a placeholder for the index_nodes attribute
        self.index_edges = None  # Add a placeholder for the index_edges attribute

    @staticmethod
    def register_table_metadata(*_, **__) -> None:
        """register table metadata"""
        return None

    @staticmethod
    def register_table(*_, **__) -> None:
        """register table"""
        return None

    @staticmethod
    def remove_table(*_, **__) -> None:
        """remove table"""
        return None

    @staticmethod
    def pull_table_metadata(*_, **__) -> None:
        """pull table metadata"""
        return None

    @staticmethod
    def push_table_metadata(*_, **__) -> None:
        """push table metadata"""
        return None

    @staticmethod
    def delete_table_metadata(*_, **__) -> None:
        """delete table metadata"""
        return None

    @staticmethod
    def query_table_metadata(*_, **__) -> list[dict]:
        """query table metadata"""
        return []

    @staticmethod
    def query_column_metadata(*_, **__) -> list[dict]:
        """query column metadata"""
        return []

    @staticmethod
    def query_all_columns_by_filepath(*_, **__) -> list[dict]:
        """query all columns by filepath"""
        return []

    @staticmethod
    def get_column_metadata_exactly(*_, **__) -> dict:
        """get column metadata exactly"""
        return {}

    @staticmethod
    def add_relationship(*_, **__) -> None:
        """add relationship"""
        return None

    @staticmethod
    def add_metadata_node(*_, **__) -> None:
        """add metadata node"""
        return None

    @staticmethod
    def update_metadata(*_, **__) -> None:
        """update metadata"""
        return None


def _require_agent_config_manager(agent_config_manager: Any | None, *, caller: str) -> Any:
    """
    Require an explicit per-Agent ConfigManager (no module-level global fallback).

    Args:
        agent_config_manager: ConfigManager from Runtime or ToolExecutionContext.
        caller: Calling site name for error messages.

    Returns:
        The given ConfigManager instance.

    Raises:
        RuntimeError: When ``agent_config_manager`` is omitted on Agent runtime paths.
    """
    if agent_config_manager is None:
        raise RuntimeError(
            f"{caller} requires a per-Agent ConfigManager; "
            "pass config_manager from Runtime.config_manager or ToolExecutionContext."
        )
    return agent_config_manager


class MemoryInstanceCacheKey(NamedTuple):
    """Cache key components for :class:`MemoryFactory` instance deduplication."""

    rag_id: str
    path_prefix: str
    config_manager_id: int
    lt_backend: Any
    lt_url: Any
    st_backend: Any
    st_url: Any


def _memory_instance_cache_key(rag_id: str, path_prefix: str, cm: Any) -> MemoryInstanceCacheKey:
    """Build MemoryFactory cache key so different Agent configs do not share instances."""
    mem_cfg = cm.get("MEMORY", {}) or {}
    lt_cfg = mem_cfg.get("long_term_storage") or {}
    st_cfg = mem_cfg.get("short_term_storage") or {}
    return MemoryInstanceCacheKey(
        str(rag_id),
        str(path_prefix),
        id(cm),
        lt_cfg.get("backend"),
        lt_cfg.get("url"),
        st_cfg.get("backend"),
        st_cfg.get("url"),
    )


def is_memory_enabled(config_manager: Any | None = None) -> bool:
    """
    判断当前配置下是否启用基于 Memory/ES 的能力。

    统一的启用条件：
    - MEMORY 有配置；
    - MEMORY.enabled 未被显式设置为 False；
    - long_term_storage 或 short_term_storage 至少配置了一个 backend。

    Args:
        config_manager: Per-Agent ConfigManager from Runtime or ToolExecutionContext (required).
    """
    cm = _require_agent_config_manager(config_manager, caller="is_memory_enabled")
    mem_cfg = cm.get("MEMORY", {}) or {}
    lt_cfg = mem_cfg.get("long_term_storage") or {}
    st_cfg = mem_cfg.get("short_term_storage") or {}
    has_external = bool(lt_cfg.get("backend") or st_cfg.get("backend"))
    return bool(mem_cfg) and bool(mem_cfg.get("enabled", True)) and has_external


class MemoryFactory:
    """
    Factory class to manage Memory instances for different users.
    Each user gets their own Memory instance with isolated data.
    """

    _instances = {}
    _lock = threading.Lock()

    @classmethod
    def get_memory(
        cls,
        rag_id: str | None = None,
        path_prefix: str | None = None,
        config_manager: Any | None = None,
    ) -> "Memory":
        """
        Get or create a Memory instance for a specific user.

        Args:
            rag_id (str | None): RAG identifier.
            path_prefix (str | None): Working directory prefix (Default: `""`).
            config_manager: Per-Agent ConfigManager from Runtime or ToolExecutionContext.

        Returns:
            Memory, Memory instance for the user.
        """
        cm = _require_agent_config_manager(config_manager, caller="MemoryFactory.get_memory")
        if rag_id is None:
            rag_id = cm.get("MEMORY.index_prefix", "")
        if path_prefix is None:
            path_prefix = cm.get("MEMORY.path_prefix", "")
        if not path_prefix:
            path_prefix = ""
        cache_key = _memory_instance_cache_key(str(rag_id), str(path_prefix), cm)
        with cls._lock:
            if cache_key not in cls._instances:
                sanitized_rag_id = str(rag_id).replace("-", "_")
                cls._instances[cache_key] = Memory(
                    path_prefix=path_prefix,
                    index_prefix=sanitized_rag_id,
                    config_manager=cm,
                )

            return cls._instances[cache_key]

    @classmethod
    def clear_user_memory(cls, user_id: str) -> None:
        """
        Clear Memory instances whose cache key ``rag_id`` matches ``user_id``.

        ``MemoryFactory`` cache keys are tuples ``(rag_id, path_prefix, id(cm), ...)``.
        Legacy callers pass ``user_id`` where the first key segment is the RAG / user scope id.

        Args:
            user_id (str): RAG id or user scope id (matched against ``cache_key[0]``).
        """
        target = str(user_id)
        with cls._lock:
            keys_to_remove = [key for key in cls._instances if key and str(key[0]) == target]
            for key in keys_to_remove:
                del cls._instances[key]

    @classmethod
    def clear_all_memories(cls) -> None:
        """
        Clear all Memory instances.
        """
        with cls._lock:
            cls._instances.clear()


class Memory:
    """
    Main class of memory management module. Create and maintain two graphs G1 (metadata & tools), G2 (knowledge graph).
    """

    def __init__(
        self,
        path_prefix: str = "",
        index_prefix: str = "",
        config_manager: Any | None = None,
    ) -> None:
        """
        Initialize the memory class and binds to knowledgebase, metadata, tool and log module.

        Args:
            path_prefix (str, optional): Working directory prefix of user local edge and node storage
                (Default: `MEMORY.path_prefix`).
            index_prefix (str, optional): Prefix of index used for knowledgebase, metadata management and tool
                management module (Default: `"CORE.perceptor.knowledge_base.index_prefix"`).
            config_manager: Per-Agent ConfigManager; required on Agent runtime paths via MemoryFactory.
        """
        cm = _require_agent_config_manager(config_manager, caller="Memory.__init__")
        self._config_manager = cm
        mem_cfg = cm.get("MEMORY", {}) or {}
        lt_cfg = mem_cfg.get("long_term_storage") or {}
        st_cfg = mem_cfg.get("short_term_storage") or {}
        long_term_url = lt_cfg.get("url")
        long_term_backend = lt_cfg.get("backend")
        short_term_backend = st_cfg.get("backend")
        self._embedding_model = cm.get("MEMORY.embedding_model")

        if not is_memory_enabled(config_manager=cm):
            # 支持禁用 Memory，避免依赖 ES/PostgreSQL：提供正式的空实现类，保持接口一致。
            self.enabled = False
            self.index_prefix = index_prefix or str(mem_cfg.get("index_prefix", "") or "")
            self.path_prefix = path_prefix or str(mem_cfg.get("path_prefix", "") or "")
            # 在 quickstart 模式下，kb/metadata 不会被实际调用
            self.kb = NullKb()  # 使用 NullKb 作为 KnowledgeBase 的空实现
            self.metadata = NullMetadata()  # 使用 NullMetadata 作为 MetadataManagement 的空实现

            self.tool = NullTool()
            self.graph_path = ""
            logger.debug("MEMORY 未启用")
            return

        # 正常启用 Memory：按原逻辑绑定 ES / PostgreSQL 等外部存储
        self.enabled = True
        if not long_term_backend and not short_term_backend:
            raise ValueError(
                "MEMORY.long_term_storage.backend or MEMORY.short_term_storage.backend "
                "must be set in per-Agent config when Memory is enabled."
            )

        if not path_prefix:
            path_prefix = mem_cfg.get("path_prefix")

        if not index_prefix:
            index_prefix = mem_cfg.get("index_prefix")

        self.index_prefix = index_prefix
        self.path_prefix = path_prefix
        embed_model = self._embedding_model
        has_long_term = bool(long_term_backend and long_term_url)

        if has_long_term:
            self.kb = KnowledgeBase(
                index=f"{index_prefix}_kb",
                hostaddress=long_term_url,
                storage_type=long_term_backend,
                embedding_model=embed_model,
            )
            self.metadata = MetadataManagement(
                index=f"{index_prefix}_meta",
                hostaddress=long_term_url,
                storage_type=long_term_backend,
                embedding_model=embed_model,
            )
            self.tool = ToolManagement(
                index=f"{index_prefix}_tool",
                hostaddress=long_term_url,
                storage_type=long_term_backend,
                embedding_model=embed_model,
            )
        else:
            self.kb = NullKb()
            self.metadata = NullMetadata()
            self.tool = NullTool()

        self.graph_path = os.path.join(path_prefix, index_prefix, "graph")

    @staticmethod
    def multi_source_bfs(G: nx.DiGraph, sources: set[int], depth: int = 0) -> tuple[set[int], set[tuple[int, int]]]:
        """
        Perform multi-source breadth-first search (BFS) on a directed graph up to a specified depth.

        Args:
            G (nx.DiGraph): The directed graph to search.
            sources (set[int]): Set of source node IDs to start the BFS from.
            depth (int): Maximum depth to search from the source nodes.

        Returns:
            Tuple[set[int], set[tuple[int, int]]], set of visited nodes and set of traversed edges.
        """
        from collections import deque

        nodes = sources.copy()
        edges = set()
        dist = dict.fromkeys(sources, 0)
        q = deque(sources)
        while q:
            u = q.popleft()
            du = dist[u]
            for v in nx.neighbors(G, u):
                edges.add((u, v))
                if v in nodes or du >= depth:
                    continue
                nodes.add(v)
                dist[v] = du + 1
                q.append(v)
        edges = {(u, v) for (u, v) in edges if u in nodes and v in nodes}
        return nodes, edges

    @staticmethod
    def path_to_edges(
        G: nx.DiGraph,
        source: str,
        paths: list,
    ) -> list[list]:
        """
        Convert paths to edges information.

        Args:
            G (nx.DiGraph): The graph containing the nodes and edges.
            source (str): The label of the source node.
            paths (list): List of paths, where each path is a list of node IDs.

        Returns:
            List[list], edges information for each path.
        """
        out = []
        for path in paths:
            edges = []
            source_label = source
            for i in range(1, len(path)):
                target_id = path[i]
                target_node = G.nodes[target_id]
                edge_data = G.get_edge_data(path[i - 1], target_id)
                if target_node["type"] == "column":
                    file_node = next(
                        u for u, _, data in G.in_edges(target_id, data=True) if data.get("relationship") == "has_column"
                    )
                    target_label = f"{G.nodes[file_node]['label']}::{target_node['label']}"
                else:
                    target_label = target_node["label"]

                edges.append(
                    {"source": source_label, "target": target_label, "relationship": edge_data["relationship"]}
                )
                source_label = target_label

            out.append(edges)
        return out

    @staticmethod
    def parse_kb_nodes(json_data: dict, ontology: str, json_path: str, next_id: int) -> tuple[list[dict], dict]:
        """
        Transform JSON nodes into node dictionaries.

        Args:
            json_data (dict): JSON data containing nodes.
            ontology (str): User provided ontology.
            json_path (str): Path to the JSON file.
            next_id (int): Next available node ID.

        Returns:
            tuple[list[dict], dict]: List of node dictionaries and mapping from original node IDs to
        """
        nodes_data = []
        id_map = {}
        for node in json_data.get("nodes", []):
            node_dict = {
                "id": next_id,
                "type": node["type"],
                "label": node["label"],
                "description": node.get("description", ""),
                "properties": node.get("properties", {}),
                "annotation": node.get(f"{ontology}_annotation", {}),
                "path": json_path,
            }
            nodes_data.append(node_dict)
            id_map[node["id"]] = next_id
            next_id += 1
        return nodes_data, id_map

    @staticmethod
    def parse_kb_edges(json_data: dict, id_map: dict, json_path: str) -> list[dict]:
        """
        Transform JSON edges into edge dictionaries.

        Args:
            json_data (dict): JSON data containing edges.
            id_map (dict): Mapping from original node IDs to new node IDs.
            json_path (str): Path to the JSON file.

        Returns:
            list[dict]: List of edge dictionaries.
        """
        edges_data = []
        for edge in json_data.get("edges", []):
            edge_dict = {
                "source": id_map.get(edge.get("source", ""), ""),
                "target": id_map.get(edge.get("target", ""), ""),
                "relationship": edge.get("relationship", ""),
                "description": edge.get("description", ""),
                "path": json_path,
            }
            edges_data.append(edge_dict)
        return edges_data

    @staticmethod
    def bfs(G: nx.DiGraph, **kwargs) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
        """
        Perform multi-source BFS on the directed graph.

        Args:
            G (nx.DiGraph): The directed graph.
            **kwargs: Additional keyword arguments, including:
                - sources (set[int]): The set of source node IDs.
                - depth (int): The depth to which BFS should be performed.

        Returns:
            Tuple[dict[int, dict[str, Any]], list[dict[str, Any]]], a tuple containing:
                - A dictionary of node IDs and their attributes.
                - A list of edges with source, target, and attributes.
        """
        sources = kwargs.get("sources", set())
        depth = kwargs.get("depth", 0)
        node_ids, edge_ids = Memory.multi_source_bfs(G, sources, depth)
        nodes = {n: {"id": n, **G.nodes[n]} for n in node_ids}
        edges = [{"source": u, "target": v, **G.edges[u, v]} for (u, v) in edge_ids]
        return nodes, edges

    def register_knowledge(
        self,
        file_type: str,
        markdown_path: str | None = None,
        user_prompt: str | None = None,
        json_path: str | None = None,
        ontology: str = "",
    ) -> bool:
        """
        Register markdown or user prompt into knowledge base, and update G2 persistent storage.

        Args:
            file_type (str): Knowledge register type, one of the following ("markdown", "user_query").
            markdown_path (str, optional): User provided markdown file path to be registered (Default: `None`).
            user_prompt (str, optional): User prompt to be registered (Default: `None`).
            json_path (str, optional): User provided JSON file path to be registered (Default: `None`).
            ontology (str): User provided ontology to be registered (Default: `""`).

        Returns:
            Bool, True if registration was successful, False otherwise.
        """
        if self.kb.index_texts is None:
            logger.error("KnowledgeBase storage is not initialized.")
            return False
        if self.kb.storage is None:
            logger.error("KnowledgeBase storage is not initialized.")
            return False
        try:
            if file_type == "markdown":
                # check file path and existence
                if not markdown_path:
                    logger.error("markdown_path为空")
                    return False

                if not os.path.exists(markdown_path):
                    logger.error(f"markdown文件不存在: {markdown_path}")
                    return False

                try:
                    self.kb.process_markdown(markdown_path=markdown_path)
                except Exception as e:
                    logger.error(f"处理markdown文件失败: {markdown_path}, 错误: {e}")
                    logger.error(f"error traceback: {traceback.format_exc()}")
                    return False

                try:
                    documents = self.kb.storage.query_exact(
                        table_name=self.kb.index_texts,
                        query_schema_list=["path"],
                        query_text_list=[markdown_path],
                        query_type="AND",
                        topk=1,
                    )
                    return len(documents) > 0
                except Exception as e:
                    logger.error(f"查询markdown文档失败: {markdown_path}, 错误: {e}")
                    logger.error(f"error traceback: {traceback.format_exc()}")
                    return False

            elif file_type == "user_query":
                # check file path and existence
                if not json_path:
                    logger.error("json path is None")
                    return False

                if not os.path.exists(json_path):
                    logger.error(f"json file does not exist: {json_path}")
                    return False

                try:
                    if user_prompt:
                        self.kb.process_user_query(user_prompt=user_prompt, filepath=json_path)

                    if json_path.endswith(".json"):
                        self.parse_kb(json_path=json_path, ontology=ontology)
                except Exception as e:
                    logger.error(f"处理user_query失败: json_path={json_path}, user_prompt={user_prompt}, 错误: {e}")
                    logger.error(f"error traceback: {traceback.format_exc()}")
                    return False

                # Verify
                try:
                    documents = self.kb.storage.query_exact(
                        table_name=self.kb.index_texts,
                        query_schema_list=["path"],
                        query_text_list=[json_path],
                        query_type="AND",
                        topk=1,
                    )
                    return len(documents) > 0
                except Exception as e:
                    logger.error(f"查询json文档失败: {json_path}, 错误: {e}")
                    logger.error(f"error traceback: {traceback.format_exc()}")
                    return False
            else:
                raise ValueError(f"Unknown file type: {file_type}. Supported types are 'markdown' and 'user_query'.")
        except Exception as e:
            logger.error(f"Knowledge registration failed: {e}")
            logger.error(f"Error traceback: {traceback.format_exc()}")
            return False

    def retrieve_table_knowledge(
        self, table_path: str, table_source_type: str, n: int = 1
    ) -> tuple[pd.DataFrame, list[str]]:
        """
        Retrieve relevant knowledge of a table from knowledge base.

        Args:
            table_path (str): Full path of a table.
            table_source_type (str): Type of table source, one of the following ('localfile', 'MySQL', 'PostgresQL').
            n (int): Number of pieces of knowledge to be extracted (Default: `1`).

        Returns:
            Tuple[pd.DataFrame, list[str]], pandas dataframe of a table, and list of extracted pieces of knowledge.
        """
        if table_source_type == "localfile":
            if table_path.endswith(".csv"):
                df = pd.read_csv(table_path, keep_default_na=False)
                unnamed_cols = [col for col in df.columns if str(col).startswith("Unnamed:")]
                if unnamed_cols:
                    raise ValueError(f"csv contains unnamed columns: {unnamed_cols}, please check the file format.")
            else:
                raise ValueError("Only support .csv format in registering local files.")
        elif table_source_type == "MySQL":
            url, table_name = table_path.rpartition("/")[0], table_path.rpartition("/")[-1]
            mysql = MySQLReader(url=url)
            df = mysql.load_table(table_name=table_name)
        else:
            raise ValueError(
                f"Not supported table_source_type '{table_source_type}'. "
                "The supported types are 'localfile' and 'MySQL'."
            )

        query = ", ".join(list(df.columns))
        query_result = self.kb.query_document(query_text=query, mode="vector", topk=n)
        knowledge = [i["doc_content"]["info"] for i in query_result]
        return df, knowledge

    def register_table(
        self, table_path: str, provided_meta: dict | None = None, table_source_type: str = "localfile"
    ) -> bool:
        """
        Register table into metadata management module, and extract file/column relationships.

        Args:
            table_path (str): Full path of a table to be registered.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `None`).
            table_source_type (str): Type of table source (Default: `"localfile"`).

        Returns:
            Bool, True if registration was successful, False otherwise.
        """
        try:
            if table_path in self.metadata.metadata:
                return True
            if provided_meta is None:
                provided_meta = {}
            df_knowledge = self.retrieve_table_knowledge(
                table_path=table_path, n=1, table_source_type=table_source_type
            )
            self.metadata.register_table(
                table_path=table_path,
                provided_meta=provided_meta,
                file_type=table_source_type,
                df_knowledge=df_knowledge,
            )
            self.metadata.push_table_metadata()
            # verify
            return table_path in self.metadata.metadata
        except Exception as e:
            logger.error(f"Table registration failed: {e}")
            logger.error(f"error traceback: {traceback.format_exc()}")
            return False

    def register_tool(self, toolname: str, provided_meta: dict | None = None) -> bool:
        """
        Register tool into tool management module.

        Args:
            toolname (str): Name of the tool.
            provided_meta (dict): User provided tool information to be auto-filled into tool management module
                (Default: `None`).

        Returns:
            Bool, True if registration was successful, False otherwise.
        """
        if toolname in self.tool.tool:
            return True
        if provided_meta is None:
            provided_meta = {}
        self.tool.register_tool(toolname=toolname, provided_meta=provided_meta)
        self.tool.push_tool_metadata()
        # verify
        return toolname in self.tool.tool

    def remove_knowledge(self, type_: str, file_path: str) -> None:
        """
        Remove knowledge information, and update G2 persistent storage.

        Args:
            type_ (str): Knowledge type to be removed, one of the following ("graph", "document").
            file_path (str): Full file path of the knowledge to be removed.
        """
        if type_ == "graph":
            self.kb.delete_graph(filepath=file_path)
        elif type_ == "document":
            self.kb.delete_document(query_schema_list=["path"], query_text_list=[file_path], query_type="AND")
        else:
            raise ValueError(f"Unknown type: {type_}. Supported types are 'graph' and 'document'.")

    def remove_table(self, table_path: str) -> None:
        """
        Remove table metadata information, and update G1 persistent storage.

        Args:
            table_path (str): Full path of the table to be removed.
        """
        if self.metadata.index_nodes is None:
            logger.error("Metadata storage is not initialized.")
            return
        if self.metadata.storage is None:
            logger.error("Metadata storage is not initialized.")
            return
        columnids = self.metadata.storage.query_exact(
            table_name=self.metadata.index_nodes,
            query_schema_list=["path"],
            query_text_list=[table_path],
            query_type="AND",
            topk=10000,
        )
        if self.tool.storage and self.tool.index_edges and self.tool.storage.query_all(self.tool.index_edges):
            for cur_columnids in columnids:
                self.tool.storage.delete_data(
                    table_name=self.tool.index_edges,
                    query_schema_list=["source", "target"],
                    query_text_list=[cur_columnids["id"], cur_columnids["id"]],
                    query_type="OR",
                )

        self.metadata.remove_table(table_path)

    def remove_tool(self, toolname: str) -> None:
        """
        Remove tool information, and update G1 persistent storage.

        Args:
            toolname (str): Full toolname of the tool to be removed.
        """
        self.tool.remove_tool(toolname)

    def query_knowledge(self, type_: str, **kwargs) -> list[dict]:
        """
        Use query_document() function in the KnowledgeBase class.

        Args:
            type_ (str): Query type for knowledge, one of the following ('text', 'graph_node').
            **kwargs: Key-word arguments in self.kb.query_document(), including ('index_name', 'mode', 'query_schema',
                'query_text', 'topk').

        Returns:
            List[dict], retrieved documents from self.kb.query_document().
        """
        if type_ == "text":
            out = self.kb.query_document(index_name=self.kb.index_texts, **kwargs)
        elif type_ == "graph_node":
            out = self.kb.query_document(index_name=self.kb.index_nodes, **kwargs)
        else:
            raise ValueError(f"Unknown type: {type_}. Supported types are 'text' and 'graph_node'.")
        return out

    def query_table(self, type_: str, **kwargs) -> list[dict] | dict:
        """
        Use query_table_metadata() or query_column_metadata() function in the MetadataManagement class.

        Args:
            type_ (str): Metadata query type, one of the following ("table", "column", "all_columns",
                "table_and_column").
            **kwargs: Key-word arguments in self.metadata.query_table_metadata(), self.metadata.query_column_metadata,
                self.metadata.query_all_columns_by_filepath() or self.metadata.get_column_metadata_exactly(), including
                ('index_name', 'mode','query_schema', 'query_text', 'similarity_threshold', 'topk', 'filepath',
                'column_name').

        Returns
            Union[list[dict], dict], retrieved metadata information from self.query_table_metadata(),
                self.query_column_metadata(), self.query_all_columns_by_filepath() or
                self.get_column_metadata_exactly().
        """
        if type_ == "table":
            out = self.metadata.query_table_metadata(**kwargs)
        elif type_ == "column":
            out = self.metadata.query_column_metadata(**kwargs)
        elif type_ == "all_columns":
            out = self.metadata.query_all_columns_by_filepath(**kwargs)
        elif type_ == "table_and_column":
            out = self.metadata.get_column_metadata_exactly(**kwargs)
        else:
            raise ValueError(
                f"Unknown type: {type_}. Supported types are 'table', 'column', 'all_columns' and 'table_and_column'."
            )

        return out

    def query_tool(self, **kwargs) -> list[dict]:
        """
        Use query_tool_metadata() function in the ToolManagement class.

        Args:
            **kwargs: Key-word arguments in self.tool.query_tool_metadata(), including ('index_name', 'mode',
                'query_schema', 'query_text', 'similarity_threshold', 'topk').

        Returns:
            List[dict], retrieved tool metadata from self.tool.query_tool_metadata().
        """
        out = self.tool.query_tool_metadata(**kwargs)
        return out

    def query_graph(self, graph: str, type_: str, **kwargs) -> list[dict]:
        """
        Use networkx to query nodes in G1, G2 or G3 graph.

        Args:
            graph (str): Type of the graph to be queried, one of the following ("G1", "G2", "G3").
            type_ (str): Type of the query to be performed, one of the following ("get_nodes_by_label",
                "get_nodes_at_distance", "bfs", "get_nodes_by_edge_type").
            **kwargs: Key-word arguments in get_nodes_by_label(), get_nodes_at_distance() or get_nodes_by_edge_type().

        Returns:
            List[dict], retrieved nodes information from the graph.
        """
        query_map = {
            "get_nodes_by_label": self.get_nodes_by_label,
            "get_nodes_at_distance": self.get_nodes_at_distance,
            "bfs": Memory.bfs,
            "get_nodes_by_edge_type": self.get_nodes_by_edge_type,
        }
        if type_ not in query_map:
            raise ValueError(f"Invalid query type: {type_}. Supported types are {list(query_map.keys())}")

        G = self.build_graph(graph)
        result = query_map[type_](G, **kwargs)
        return result

    def query_edges(
        self, graph: str, source: str, target: str, topk: int, is_potential: bool = False
    ) -> list[list[dict]]:
        """
        Query topk shortest paths from source to target in G1 or G2 graph.

        Args:
            graph (str): Type of the graph to be queried, one of the following ("G1", "G2").
            source (str): Source node of the edge.
            target (str): Target node of the edge.
            topk (int): Number of top paths to return.
            is_potential (bool, optional): Whether to include potential edges in the graph (Default: `False`).

        Returns:
            List[list[dict]], retrieved edges information from the graph.
        """
        G = self.build_graph(graph, is_potential)
        source_info = self.get_nodes_by_label(G, label=source)
        target_info = self.get_nodes_by_label(G, label=target)
        if not source_info or not target_info:
            return []

        source_id = source_info[0]["id"]
        target_id = target_info[0]["id"]

        try:
            paths = list(nx.shortest_simple_paths(G, source=source_id, target=target_id))[:topk]
        except Exception as e:
            logger.warning(f"Cannot find any path between given nodes: {e}.")
            return []

        return Memory.path_to_edges(G, source, paths)

    def global_search(self, user_query: str, topk: int = 5) -> list[dict]:
        """
        Search for semantically relevant reports in hierarchical community reports.

        Args:
            user_query (str): User's natural language query.
            topk (int, optional): Number of top results to return (Default: `5`).

        Returns:
            List[dict], retrieved reports information from the knowledge base.
        """
        results = self.kb.query_document(
            index_name=self.kb.index_reports, mode="vector", query_schema="info", query_text=user_query, topk=topk
        )
        for result in results:
            del result["doc_content"]["info_embedding"]

        return results

    def infer_potential_joinable(self, relation: str = "is_joinable_with") -> None:
        """
        Infer potential joinable relation in nx.DiGraph. This function is also applicable for other bidirectional
            transitive relations.

        Args:
            relation (str): The relation to infer (Default: `is_joinable_with`).
        """
        G = self.build_graph("G1")
        base_edges = {(u, v) for u, v, attr in G.edges(data=True) if attr["relationship"] == relation}
        bidirection_edges = base_edges | {(v, u) for u, v in base_edges}
        subG = nx.DiGraph()
        subG.add_edges_from(bidirection_edges)
        closure = nx.transitive_closure(subG)
        potential_edges = set()
        for u, v in closure.edges():
            if u != v and (u, v) not in base_edges:
                pre_u = [
                    id
                    for id, _, attr in G.in_edges(u, data=True)  # type: ignore
                    if attr.get("relationship", "") == "has_column"
                ]
                pre_v = [
                    id
                    for id, _, attr in G.in_edges(v, data=True)  # type: ignore
                    if attr.get("relationship", "") == "has_column"
                ]
                if not pre_u or not pre_v:
                    continue
                if pre_u[0] != pre_v[0]:
                    potential_edges.add((u, v))
        self.update_storage_potential(node_type="metadata", potential_edges=potential_edges, relation=relation)
        self.metadata.pull_table_metadata()

    def show_graph(self, graph: str, show_potential: bool = False) -> None:
        """
        Generate HTML of the specified graph.

        Args:
            graph (str): Type of relationships to be extracted, one of the following ("G1", "G2", "G3").
            show_potential (bool): Whether to show potential relationships (Default: `False`).
        """
        if graph not in ["G1", "G2", "G3"]:
            raise ValueError(f"Invalid graph type: {graph}. Supported graphs are ('G1', 'G2', 'G3')")

        try:
            os.makedirs(self.graph_path, exist_ok=True)
            G = self.build_graph(graph, show_potential)
            config = html_config(graph, G)
            graph_to_html(config, G, os.path.join(self.graph_path, f"{graph}.html"))
        except Exception as e:
            logger.error(f"生成图谱失败: {e}")
            logger.error(f"error traceback: {traceback.format_exc()}")

    def parse_kb(self, json_path: str, ontology: str = "") -> bool:
        """
        Transform JSON into nodes and edges, and add into knowledgebase storage.

        Args:
            json_path (str): Full path to the JSON file to be processed.
            ontology (str): User provided ontology to be registered (Default: `""`).

        Returns:
            bool: True if registration was successful, False otherwise.
        """
        with open(json_path, encoding="utf-8") as f:
            json_data = json.load(f)

        if "nodes" not in json_data or len(json_data.get("nodes", [])) == 0:
            return True
        if self.kb.storage is None:
            logger.error("KnowledgeBase storage is not initialized.")
            return False
        if self.kb.index_nodes is None:
            logger.error("KnowledgeBase index_nodes is not defined.")
            return False
        node_id = [i["id"] for i in self.kb.storage.query_all(table_name=self.kb.index_nodes)]
        next_id = max(int(i) for i in node_id) + 1 if node_id else 0
        nodes_data, id_map = Memory.parse_kb_nodes(
            json_data=json_data, ontology=ontology, json_path=json_path, next_id=next_id
        )

        if nodes_data:
            nodes_df = pd.DataFrame(nodes_data)
            self.kb.insert_graph(nodes=nodes_df.astype(str))

        edges_data = Memory.parse_kb_edges(json_data=json_data, id_map=id_map, json_path=json_path)

        if edges_data:
            edges_df = pd.DataFrame(edges_data)
            self.kb.insert_graph(edges=edges_df.astype(str))

        return True

    def get_g3_nodes_edges(
        self,
    ) -> tuple[list[dict], list[dict]]:
        """
        Get nodes and edges for G3 graph by combining metadata, tool and knowledge graph nodes and edges.

        Returns:
            Tuple[list[dict], list[dict]], list of nodes and list of edges for G3 graph.
        """
        if self.metadata.index_nodes is None or self.tool.index_nodes is None or self.kb.index_nodes is None:
            raise ValueError("Metadata, Tool or KnowledgeBase index nodes is not defined.")
        if self.metadata.index_edges is None or self.tool.index_edges is None or self.kb.index_edges is None:
            raise ValueError("Metadata, Tool or KnowledgeBase index edges is not defined.")
        if self.metadata.storage is None or self.tool.storage is None or self.kb.storage is None:
            raise ValueError("Metadata, Tool or KnowledgeBase storage is not initialized.")

        DRIFT = 10000  # To avoid ID conflicts, add OFFSET to knowledge graph node IDs.
        meta_nodes = self.metadata.storage.query_all(table_name=self.metadata.index_nodes)
        tool_nodes = self.tool.storage.query_all(table_name=self.tool.index_nodes)
        kb_nodes = self.kb.storage.query_all(table_name=self.kb.index_nodes)
        meta_edges = self.metadata.storage.query_relationship(
            table_name=self.metadata.index_edges, exists_field="relationship"
        )
        tool_edges = self.tool.storage.query_relationship(table_name=self.tool.index_edges, exists_field="relationship")
        kb_edges = self.kb.storage.query_all(table_name=self.kb.index_edges)
        if self.kb.index_unified_semantics:
            unified_edges = self.kb.storage.query_all(table_name=self.kb.index_unified_semantics)
        else:
            unified_edges = []
        for n in kb_nodes:
            n["id"] = int(n["id"]) + DRIFT
        for e in kb_edges:
            e["source"] = int(e["source"]) + DRIFT
            e["target"] = int(e["target"]) + DRIFT
        for e in unified_edges:
            e["source"] = int(e["knowledge"]) + DRIFT
            e["target"] = int(e["metadata"])
        nodes = meta_nodes + tool_nodes + kb_nodes
        edges = meta_edges + tool_edges + kb_edges + unified_edges
        return nodes, edges

    def build_graph(self, graph: str, show_potential: bool = False) -> nx.DiGraph | nx.MultiDiGraph:
        """
        Build a directed graph using networkx from nodes and edges in elasticsearch.

        Args:
            graph (str): Type of graph to be built, one of the following ("G1", "G2", "G3").
            show_potential (bool): Whether to show potential relationships in the graph (Default: False).

        Returns:
            Union[nx.DiGraph, nx.MultiDiGraph], a directed graph or multi-directed graph.
        """
        if graph not in ["G1", "G2", "G3"]:
            raise ValueError(f"Invalid graph type: {graph}. Supported graphs are ('G1', 'G2', 'G3')")
        if self.metadata.storage is None or self.tool.storage is None or self.kb.storage is None:
            raise ValueError("Metadata, Tool or KnowledgeBase storage is not initialized.")
        if self.metadata.index_nodes is None or self.tool.index_nodes is None or self.kb.index_nodes is None:
            raise ValueError("Metadata, Tool or KnowledgeBase index nodes is not defined.")
        if self.metadata.index_edges is None or self.tool.index_edges is None or self.kb.index_edges is None:
            raise ValueError("Metadata, Tool or KnowledgeBase index edges is not defined.")
        if graph == "G1":
            nodes = self.metadata.storage.query_all(table_name=self.metadata.index_nodes) + self.tool.storage.query_all(
                table_name=self.tool.index_nodes
            )
            if show_potential:
                edges = self.metadata.storage.query_all(
                    table_name=self.metadata.index_edges
                ) + self.tool.storage.query_all(table_name=self.tool.index_edges)
            else:
                edges = self.kb.storage.query_relationship(
                    table_name=self.metadata.index_edges, exists_field="relationship"
                ) + self.kb.storage.query_relationship(table_name=self.tool.index_edges, exists_field="relationship")
        elif graph == "G2":
            nodes = self.kb.storage.query_all(table_name=self.kb.index_nodes)
            edges = self.kb.storage.query_all(table_name=self.kb.index_edges)
        elif graph == "G3":
            nodes, edges = self.get_g3_nodes_edges()

        G = nx.MultiDiGraph() if show_potential else nx.DiGraph()
        for node in nodes:
            node_attrs = {k: v for k, v in node.items() if k != "id" and not k.endswith("_embedding")}
            G.add_node(int(node["id"]), **node_attrs)

        for edge in edges:
            edge_attrs = {}
            for k, v in edge.items():
                if k not in ["source", "target"] and not k.endswith("_embedding"):
                    edge_attrs[k] = v
            G.add_edge(int(edge["source"]), int(edge["target"]), **edge_attrs)

        return G

    def ask_graph(self, user_prompt: str) -> str:
        """
        Respond to user query about the knowledge graph.

        Args:
            user_prompt (str): Given user query.

        Returns:
            Dict, inference model response to the user query.
        """
        if self.kb.storage is None:
            raise ValueError("KnowledgeBase storage is not initialized.")
        if self.kb.index_nodes is None or self.kb.index_edges is None:
            raise ValueError("KnowledgeBase index nodes or edges is not defined.")
        all_nodes = self.kb.storage.query_all(table_name=self.kb.index_nodes)
        all_edges = self.kb.storage.query_all(table_name=self.kb.index_edges)

        node_fields = ["id", "type", "label", "description", "properties", "annotation"]
        edge_fields = ["source", "target", "relationship", "description"]

        node_info = [{k: v for k, v in i.items() if k in node_fields} for i in all_nodes]
        edge_info = [{k: v for k, v in i.items() if k in edge_fields} for i in all_edges]

        graph_content = str({"node": node_info, "edge": edge_info})
        state = {"user_prompt": user_prompt, "file_content": graph_content}
        message = PromptTemplate.from_package_relative(
            f"{PROMPT_MD_PREFIX}/knowledge_base/user_guide_kb"
        ).apply_prompt_template(**state)
        return model_inference(message)

    def update_storage_potential(self, node_type: str, potential_edges: set, relation: str) -> None:
        """
        Update the potential relationships in the Elasticsearch index.

        Args:
            node_type (str): Metadata graph potential node type, one of the following ("metadata", "tool").
            potential_edges (set): A set of potential edges to update.
            relation (str): The relation type to update.
        """
        if self.metadata.storage is None or self.tool.storage is None:
            raise ValueError("Metadata or Tool storage is not initialized.")
        if self.metadata.index_edges is None or self.tool.index_edges is None:
            raise ValueError("Metadata or Tool index edges is not defined.")
        if node_type == "metadata":
            self.metadata.storage.delete_data(
                table_name=self.metadata.index_edges,
                query_schema_list=["potential_relationship"],
                query_text_list=[relation],
                query_type="AND",
            )
            for u, v in potential_edges:
                self.metadata.storage.insert_data(
                    table_name=self.metadata.index_edges,
                    data={"source": u, "target": v, "potential_relationship": relation},
                )
        elif node_type == "tool":
            self.tool.storage.delete_data(
                table_name=self.tool.index_edges,
                query_schema_list=["potential_relationship"],
                query_text_list=[relation],
                query_type="AND",
            )
            for u, v in potential_edges:
                self.tool.storage.insert_data(
                    table_name=self.tool.index_edges,
                    data={"source": u, "target": v, "potential_relationship": relation},
                )
        else:
            raise ValueError(f"Unsupported node type {node_type}. Supported node types are 'metadata', 'tool'.")

    def get_nodes_by_label(self, G: nx.DiGraph, **kwargs) -> list[dict]:
        """
        Get nodes by label, return all nodes attributes.

        Args:
            G (nx.DiGraph): The directed graph.
            **kwargs: Additional keyword arguments, including:
                - label (str): The label of the nodes to retrieve.

        Returns:
            List[dict], a list of id and attributes of the nodes.
        """
        label = kwargs.get("label")
        if not label:
            raise ValueError("label must be provided.")

        nodes_info = []
        if "::" in label:
            filename = label.split("::")[0]
            column = label.split("::")[-1]
            try:
                node_id = self.metadata.metadata[filename]["schema"][column]["id"]
                node_info = {"id": node_id}
                node_info.update(G.nodes[node_id])
                nodes_info.append(node_info)
            except Exception as e:
                logger.warning(f"Node does not exist in local metadata copy: {e}.")
                return []
        else:
            for node_id, node_data in G.nodes(data=True):
                if node_data.get("label") == label:
                    node_info = {"id": node_id}
                    node_info.update(node_data)
                    nodes_info.append(node_info)

        return nodes_info

    def get_nodes_at_distance(self, G: nx.DiGraph, **kwargs) -> list[dict]:
        """
        Get nodes at a specific distance from the start node in a directed graph. If distance is negative, find
            predecessor nodes at distance.

        Args:
            G (nx.DiGraph): The directed graph.
            **kwargs: Additional keyword arguments, including:
                - label (str): The starting node label.
                - distance (int): The distance from the starting node.

        Returns:
            List[dict], a list of id and attributes of the nodes.
        """
        label = kwargs.get("label")
        distance = kwargs.get("distance")
        if label is None or distance is None:
            raise ValueError("Either label or distance must be provided.")

        # suppose G has many nodes with the same label
        start_nodes = self.get_nodes_by_label(G, label=label)

        # find predecessor nodes
        if distance < 0:
            G = G.reverse()
            distance = abs(distance)

        node_ids = []
        for start_node in start_nodes:
            distances = nx.single_source_shortest_path_length(G, start_node["id"], cutoff=distance)
            node_ids.extend([id for id, dist in distances.items() if dist == distance])

        nodes_info = []
        for node_id in node_ids:
            node_info = {"id": node_id}
            node_info.update(G.nodes[node_id])
            nodes_info.append(node_info)

        return nodes_info

    def get_nodes_by_edge_type(self, G: nx.DiGraph, **kwargs) -> list[dict]:
        """
        Get successor nodes connected by a specific edge type.

        Args:
            G (nx.DiGraph): The directed graph.
            **kwargs: Additional keyword arguments, including:
                - label (str): The starting node ID.
                - edge_type (str): The type of edge to filter by.

        Returns:
            List[dict], a list of id and attributes of the nodes.
        """
        label = kwargs.get("label")
        edge_type = kwargs.get("edge_type")
        if not label or not edge_type:
            raise ValueError("Both label and edge_type must be provided.")

        start_nodes = self.get_nodes_by_label(G, label=label)
        node_ids = set()
        for start_node in start_nodes:
            for successor in G.successors(start_node["id"]):
                edge_data = G.get_edge_data(start_node["id"], successor)
                if edge_data.get("relationship") == edge_type:
                    node_ids.add(successor)

        nodes_info = []
        for node_id in node_ids:
            node_info = {"id": node_id}
            node_info.update(G.nodes[node_id])
            nodes_info.append(node_info)

        return nodes_info
