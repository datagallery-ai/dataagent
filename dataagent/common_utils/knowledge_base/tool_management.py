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

import numpy as np

from dataagent.common_utils.knowledge_base.utils_common import (
    StorageConnectorElasticSearch,
)
from dataagent.common_utils.knowledge_base.utils_inference import cosine_similarity, embedding
from dataagent.common_utils.knowledge_base.utils_metadata import create_tool_schema_template

# Define schemas of tool graph nodes
# -- id: tool graph node id
# -- type: tool graph node type
# -- label: tool graph node name
# -- label_embedding: 1024D vector embedding of node name
# -- description: tool graph node description
# -- description_embedding: 1024D vector embedding of node description
# -- parameters: description of function input parameters, separated by semicolons
# -- output: description of function outputs, separated by semicolons
MAPPING_GRAPH_NODES = {
    "mappings": {
        "properties": {
            "id": {"type": "integer"},
            "type": {"type": "keyword"},
            "label": {"type": "keyword"},
            "label_embedding": {"type": "dense_vector", "dims": 1024},
            "description": {"type": "keyword"},
            "description_embedding": {"type": "dense_vector", "dims": 1024},
            "parameters": {"type": "keyword"},
            "output": {"type": "keyword"},
        }
    }
}

# Define schemas of tool graph edges
# -- source: tool graph edge source
# -- target: tool graph edge destination
# -- relationship: tool graph edge type
MAPPING_GRAPH_EDGES = {
    "mappings": {
        "properties": {
            "source": {"type": "integer"},
            "target": {"type": "integer"},
            "relationship": {"type": "keyword"},
            "potential_relationship": {"type": "keyword"},
        }
    }
}


class ToolManagement:
    """
    Main class of tool management module.
    """

    def __init__(
        self,
        hostaddress: str = "",
        storage_type: str | None = None,
        index: str = "default_tool",
        mapping_node: dict | None = None,
        mapping_edge: dict | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """
        Initialize ToolManagement class and touch storage service.

        Args:
            hostaddress (str, optional): Host address of storage service (Default: `MEMORY.long_term_storage.url`).
            storage_type (str, optional): Type of storage service (Default: `MEMORY.long_term_storage.backend`).
            index (str): Name of storage index for tool (Default: `default_tool`).
            mapping_node (dict, optional): Schema of tool node tables (Default: None).
            mapping_edge (dict, optional): Schema of tool edge tables (Default: None).
            embedding_model: MEMORY.embedding_model from per-Agent config (Agent runtime paths).
        """
        self._embedding_model = embedding_model
        mapping_node = mapping_node or MAPPING_GRAPH_NODES
        mapping_edge = mapping_edge or MAPPING_GRAPH_EDGES
        if not hostaddress or not storage_type:
            raise ValueError(
                "ToolManagement requires explicit hostaddress and storage_type from per-Agent MEMORY config."
            )

        self.hostaddress = hostaddress
        self.index_nodes = index + "_nodes"
        self.index_edges = index + "_edges"
        self.mapping_nodes = mapping_node
        self.mapping_edges = mapping_edge
        if storage_type == "elasticsearch":
            self.storage = StorageConnectorElasticSearch(hosts=hostaddress)
        else:
            raise ValueError(f'Unsupported storage type {storage_type}. Supported connectors are ["elasticsearch"].')

        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        self.storage.create_table(table_name=self.index_edges, mapping=self.mapping_edges)
        self.pull_tool_metadata()

    @staticmethod
    def get_format_from_filtered_results(
        index_name: str,
        filtered_results: list[dict],
    ) -> list[dict]:
        """
        Get return format from filtered results.

        Args:
            index_name (str): Name of storage index.
            filtered_results (list[dict]): Filtered results from storage query.

        Returns:
            List[dict], retrieved documents in the format of [{"tool_id":"...", "tool_content":"..."}, ...]
        """
        out = []
        for i in filtered_results:
            meta = {}
            for j in ["id", "label", "type", "description", "parameters", "output"]:
                meta[j] = i[j]
            out.append({"tool_id": index_name, "tool_content": meta.copy()})
        return out

    def pull_tool_metadata(self) -> None:
        """
        Pull tool metadata information from storage to local class attributes.
        """
        self.tool = {}
        tools = self.storage.query_all(table_name=self.index_nodes)
        for i in tools:
            meta = create_tool_schema_template(toolname=i["label"], startnum=i["id"])
            for j in ["type", "description", "parameters", "output"]:
                meta[i["label"]][j] = i[j]
            self.tool.update(meta)

    def push_tool_metadata(self) -> None:
        """Push local tool metadata dict into storage (overwrite)."""
        self.storage.drop_table(table_name=self.index_nodes)
        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        action_tools = []
        for k, v in self.tool.items():
            label, description = k, v.get("description", "")
            action_tools.append(
                {
                    "id": v["id"],
                    "type": v.get("type", ""),
                    "label": label,
                    "label_embedding": self._embed(label),
                    "description": description,
                    "description_embedding": self._embed(description),
                    "parameters": v.get("parameters", ""),
                    "output": v.get("output", "None"),
                }
            )
        for curtool in action_tools:
            self.storage.insert_data(table_name=self.index_nodes, data=curtool)

    def query_tool_metadata(
        self,
        index_name: str | None = None,
        mode: str = "fulltext",
        query_schema: str = "label",
        query_text: str = "",
        similarity_threshold: float = 0.5,
        topk: int = 1,
    ) -> list[dict]:
        """
        Fulltext and vector similarity search are supported.

        Args:
            index_name (str, optional): Name of storage index (Default: `self.index_tools`).
            mode (str): One of the following search mode of document query in ["fulltext", "vector"]
                (Default: `fulltext`).
            query_schema (str): Schema of tool management table to be searched in ["label", "description"]
                (Default: `label`).
            query_text (str): User query text (Default: `""`).
            similarity_threshold (float): Cosine similarity threshold between file descriptions, only used when mode is
                "vector" (Default: `0.5`).
            topk (int): Number of documents to be returned (Default: `1`).

        Returns:
            List[dict], retrieved documents in the format of [{"tool_id":"...", "tool_content":"..."}, ...]
        """
        if mode not in ["fulltext", "vector"]:
            raise ValueError("Input argument mode must be either 'fulltext' or 'vector'.")

        if query_schema not in ["label", "description"]:
            raise ValueError("Input argument query_schema must be one of the following 'label', 'description'.")

        if index_name is None:
            index_name = self.index_nodes

        if mode == "fulltext":
            filtered_results = self.storage.query_fulltext(
                table_name=index_name, query_schema=query_schema, query_text=query_text, topk=topk
            )
        else:
            vector_embedding = list(self._embed(query_text))
            results = self.storage.query_vector(
                table_name=index_name,
                query_schema=query_schema + "_embedding",
                query_vector=vector_embedding,
                topk=topk,
            )
            filtered_results = []
            for result in results:
                doc_vector = np.array(result[query_schema + "_embedding"], dtype=np.float32)
                similarity = cosine_similarity(np.array([vector_embedding]), np.array([doc_vector]))[0][0]
                if similarity >= similarity_threshold:
                    result["_similarity"] = similarity
                    filtered_results.append(result)

                if len(filtered_results) >= topk:
                    break

        return ToolManagement.get_format_from_filtered_results(index_name=index_name, filtered_results=filtered_results)

    def register_tool(self, toolname: str, provided_meta: dict | None = None) -> None:
        """
        Register a tool with relevant information, including id, label, type, description, parameters and etc.

        Args:
            toolname (str): Name of the tool.
            provided_meta (dict | None): User provided tool metadata to be auto-filled into tool metadata \
                (Default: `None`).
        """
        provided_meta = provided_meta or {}
        if toolname in self.tool:
            return
        min_id = min([v["id"] for _, v in self.tool.items()]) if self.tool else 0
        meta = create_tool_schema_template(toolname=toolname, startnum=min_id - 1)
        for i in ["type", "description", "parameters", "output"]:
            meta[toolname][i] = provided_meta.get(i, "")
        self.tool.update(meta)

    def remove_tool(self, toolname: str) -> None:
        """
        Remove tool metadata information of a table.

        Args:
            toolname (str): Name of the tool.
        """
        if toolname in self.tool:
            self.storage.delete_data(
                table_name=self.index_nodes, query_schema_list=["label"], query_text_list=[toolname], query_type="AND"
            )
            if self.storage.query_all(self.index_edges):
                self.storage.delete_data(
                    table_name=self.index_edges,
                    query_schema_list=["source", "target"],
                    query_text_list=[self.tool[toolname]["id"], self.tool[toolname]["id"]],
                    query_type="OR",
                )

            del self.tool[toolname]

    def update_tool_node(self, toolname: str, update_field: str, update_value: str) -> None:
        """
        Update a specific field of a tool node.

        Args:
            toolname (str): Name of the tool.
            update_field (str): Field to be updated, one of the following fields ('type', 'description', 'parameters',
            'output').
            update_value (str): Updated value.
        """
        if toolname not in self.tool:
            raise ValueError(f"Tool '{toolname}' is not found in the tool metadata.")
        if update_field not in ["type", "description", "parameters", "output"]:
            raise ValueError(
                "Input argument 'update_field' must be one of the following fields: type, description, "
                "parameters, output."
            )
        self.tool[toolname][update_field] = update_value
        self.storage.update_value(
            table_name=self.index_nodes,
            pos_query=["label"],
            pos_text=[toolname],
            update_field=update_field,
            update_value=update_value,
        )

    def add_tool_edge(self, sourceid: int, targetid: int, relationship: str) -> None:
        """
        Add a tool graph edge between two tool nodes or one tool node and one column node.

        Args:
            sourceid (int): Source id of the tool graph edge.
            targetid (int): Target id of the tool graph edge.
            relationship (str): Relationship type of the tool graph edge.
        """
        if sourceid == targetid:
            raise ValueError("Source id and target id cannot be the same.")

        if not self.storage.query_exact(
            table_name=self.index_edges,
            query_schema_list=["source", "target", "relationship"],
            query_text_list=[sourceid, targetid, relationship],
            query_type="AND",
            topk=1,
        ):
            edge_info = {"source": sourceid, "target": targetid, "relationship": relationship}
            self.storage.insert_data(table_name=self.index_edges, data=edge_info)

    def remove_tool_edge(self, sourceid: int, targetid: int, relationship: str) -> None:
        """
        Remove a tool graph edge between two tool nodes or one tool node and one column node.

        Args:
            sourceid (int): Source id of the tool graph edge.
            targetid (int): Target id of the tool graph edge.
            relationship (str): Relationship type of the tool graph edge.
        """
        self.storage.delete_data(
            table_name=self.index_edges,
            query_schema_list=["source", "target", "relationship"],
            query_text_list=[sourceid, targetid, relationship],
            query_type="AND",
        )

    def _embed(self, query: str | list[str]):
        """Embed text using per-Agent embedding model when configured."""
        return embedding(query, embedding_model=self._embedding_model)
