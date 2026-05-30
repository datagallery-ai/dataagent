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

import pandas as pd

from dataagent.common_utils.knowledge_base.utils_common import (
    StorageConnectorElasticSearch,
    StorageConnectorGaussVector,
)
from dataagent.common_utils.knowledge_base.utils_inference import embedding, model_inference
from dataagent.common_utils.knowledge_base.utils_knowledgebase import chunk_markdown
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate

# Define schemas of knowledge base table for search queries
# -- info: chunked piece of documents in the knowledge base
# -- info_embedding: 1024D vector embedding of chunked documents
# -- path: path to the source markdown file
MAPPING_TEXTS = {
    "mappings": {
        "properties": {
            "info": {"type": "keyword"},
            "info_embedding": {"type": "dense_vector", "dims": 1024},
            "path": {"type": "keyword"},
        }
    }
}

# Define schemas of knowledge base table for report generation
# -- info: the report of the community
# -- info_embedding: 1024D vector embedding of the report
# -- community_id: id of the community to which the report belongs
# -- level: level of the community in the hierarchy
# -- parent: id of the parent node of the community
# -- children: list of ids of the child nodes of the community
# -- node_ids: list of ids of knowledge graph nodes associated with the community
MAPPING_REPORTS = {
    "mappings": {
        "properties": {
            "info": {"type": "keyword"},
            "info_embedding": {"type": "dense_vector", "dims": 1024},
            "community_id": {"type": "integer"},
            "level": {"type": "integer"},
            "parent": {"type": "integer"},
            "children": {"type": "keyword"},
            "node_ids": {"type": "keyword"},
        }
    }
}

# Define schemas of knowledge graph nodes
# -- id: knowledge graph node id
# -- type: knowledge graph node type
# -- label: knowledge graph node name
# -- label_embedding: 1024D vector embedding of node name
# -- description: knowledge graph node description
# -- description_embedding: 1024D vector embedding of node name
# -- properties: knowledge graph node properties
# -- annotation: knowledge graph node annotations from an ontology
# -- path: path of knowledge file
MAPPING_GRAPH_NODES = {
    "mappings": {
        "properties": {
            "id": {"type": "integer"},
            "type": {"type": "keyword"},
            "label": {"type": "keyword"},
            "label_embedding": {"type": "dense_vector", "dims": 1024},
            "description": {"type": "keyword"},
            "description_embedding": {"type": "dense_vector", "dims": 1024},
            "properties": {"type": "keyword"},
            "annotation": {"type": "keyword"},
            "path": {"type": "keyword"},
        }
    }
}

# Define schemas of knowledge graph edges
# -- source: knowledge graph edge source
# -- target: knowledge graph edge destination
# -- relationship: knowledge graph edge type
# -- description: knowledge graph edge description
# -- path: path of knowledge file
MAPPING_GRAPH_EDGES = {
    "mappings": {
        "properties": {
            "source": {"type": "integer"},
            "target": {"type": "integer"},
            "relationship": {"type": "keyword"},
            "description": {"type": "keyword"},
            "path": {"type": "keyword"},
        }
    }
}

# Define schemas of unified semantics between metadata and knowledge nodes
# -- knowledge: knowledge graph node name
# -- metadata: metadata graph node name
# -- relationship: unified semantics edge type
MAPPING_GRAPH_UNIFIED_SEMANTICS = {
    "mappings": {
        "properties": {
            "knowledge": {"type": "integer"},
            "metadata": {"type": "integer"},
            "relationship": {"type": "keyword"},
        }
    }
}


class KnowledgeBase:
    """
    Main class of knowledge base.
    """

    def __init__(
        self,
        hostaddress: str = "",
        storage_type: str | None = None,
        index: str = "default_kb",
        mapping_text: dict | None = None,
        mapping_node: dict | None = None,
        mapping_edge: dict | None = None,
        mapping_report: dict | None = None,
        mapping_unified_semantics: dict | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """
        Initialize KnowledgeBase class and touch storage service.

        Args:
            hostaddress (str, optional): Host address of storage service (Default: `MEMORY.long_term_storage.url`).
            storage_type (str): Type of storage service (Default: `MEMORY.long_term_storage.backend`).
            index (str): Name of storage index for knowledge base (Default: `default_kb`).
            mapping_text (dict): Schema of knowledge base tables (Default: None).
            mapping_node (dict): Schema of knowledge graph nodes (Default: None).
            mapping_edge (dict): Schema of knowledge graph edges (Default: None).
            mapping_report (dict): Schema of knowledge graph reports (Default: None).
            mapping_unified_semantics (dict): Schema of unified semantics relationship (Default: None).
            embedding_model: MEMORY.embedding_model from per-Agent config (Agent runtime paths).
        """
        self._embedding_model = embedding_model
        mapping_text = mapping_text or MAPPING_TEXTS
        mapping_node = mapping_node or MAPPING_GRAPH_NODES
        mapping_edge = mapping_edge or MAPPING_GRAPH_EDGES
        mapping_report = mapping_report or MAPPING_REPORTS
        mapping_unified_semantics = mapping_unified_semantics or MAPPING_GRAPH_UNIFIED_SEMANTICS
        if not hostaddress or not storage_type:
            raise ValueError(
                "KnowledgeBase requires explicit hostaddress and storage_type from per-Agent MEMORY config."
            )

        self.hostaddress = hostaddress
        self.index_texts = index + "_texts"
        self.index_nodes = index + "_nodes"
        self.index_edges = index + "_edges"
        self.index_reports = index + "_reports"
        self.index_unified_semantics = index + "_unified_semantics"
        self.mapping_texts = mapping_text
        self.mapping_nodes = mapping_node
        self.mapping_edges = mapping_edge
        self.mapping_reports = mapping_report
        self.mapping_unified_semantics = mapping_unified_semantics
        self.knowledge = {"nodes": [], "edges": [], "unified_semantics": []}
        if storage_type == "elasticsearch":
            self.storage = StorageConnectorElasticSearch(hosts=hostaddress)
        elif storage_type == "gaussvector":
            self.storage = StorageConnectorGaussVector(hosts=hostaddress)
        else:
            raise ValueError(
                f'Unsupported storage type {storage_type}. Supported connectors are ["elasticsearch", "gaussvector"].'
            )

        self.storage.create_table(table_name=self.index_texts, mapping=self.mapping_texts)
        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        self.storage.create_table(table_name=self.index_edges, mapping=self.mapping_edges)
        self.storage.create_table(table_name=self.index_reports, mapping=self.mapping_reports)
        self.storage.create_table(table_name=self.index_unified_semantics, mapping=self.mapping_unified_semantics)

    def push_knowledge(self) -> None:
        """Push knowledge into storage (overwrite)."""
        self.storage.drop_table(table_name=self.index_nodes)
        self.storage.drop_table(table_name=self.index_edges)
        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        self.storage.create_table(table_name=self.index_edges, mapping=self.mapping_edges)
        for node in self.knowledge.get("nodes", []):
            label, description = node.get("label", ""), node.get("description", "")
            n = {
                "id": node["id"],
                "type": node.get("type", ""),
                "label": label,
                "label_embedding": self._embed(label),
                "description": description,
                "description_embedding": self._embed(description),
            }
            self.storage.insert_data(self.index_nodes, n)
        for edge in self.knowledge.get("edges", []):
            e = {"source": edge["source"], "target": edge["target"], "relationship": edge.get("relationship", "")}
            self.storage.insert_data(self.index_edges, e)
        for edge in self.knowledge.get("unified_semantics", []):
            e = {
                "knowledge": edge["knowledge"],
                "metadata": edge["metadata"],
                "relationship": edge.get("relationship", "mapping"),
            }
            self.storage.insert_data(self.index_unified_semantics, e)

    def insert_document(self, document: dict, index_name: str | None = None) -> None:
        """
        Insert a document into storage.

        Args:
            document (dict): Piece of information to be inserted (Default: `None`).
            index_name (str, optional): Name of storage index for knowledge base (Default: `self.index_texts`).
        """
        if index_name is None:
            index_name = self.index_texts

        self.storage.insert_data(table_name=index_name, data=document)

    def insert_graph(self, nodes: pd.DataFrame | None = None, edges: pd.DataFrame | None = None) -> None:
        """
        Insert graph nodes and edges to storage from pandas dataframe.

        Args:
            nodes (pd.DataFrame, optional): dataframe of node table (Default: `None`).
            edges (pd.DataFrame, optional): dataframe of edge table (Default: `None`).
        """
        if nodes is not None:
            for i in nodes.index:
                self.storage.insert_data(
                    table_name=self.index_nodes,
                    data=dict(nodes.loc[i])
                    | {
                        "label_embedding": self._embed(str(nodes.loc[i, "label"])),
                        "description_embedding": self._embed(str(nodes.loc[i, "description"])),
                    },
                )

        if edges is not None:
            for i in edges.index:
                self.storage.insert_data(table_name=self.index_edges, data=dict(edges.loc[i]))

    def delete_graph(self, filepath: str) -> None:
        """
        Delete knowledge graph extracted from one file path.

        Args:
            filepath (str): file path whose knowledge graph is to be deleted.
        """
        self.storage.delete_data(
            table_name=self.index_nodes, query_schema_list=["path"], query_text_list=[filepath], query_type="fulltext"
        )
        self.storage.delete_data(
            table_name=self.index_edges, query_schema_list=["path"], query_text_list=[filepath], query_type="fulltext"
        )

    def delete_document(
        self,
        query_text_list: list[str | int],
        query_type: str,
        query_schema_list: list[str] | None = None,
        index_name: str | None = None,
    ) -> None:
        """
        Delete a document by its content. Note that all documents containing the given substring will be deleted.

        Args:
            query_schema (str): Schema of knowledge base table to be searched (e.g., `info`).
            query_text (str): User query text (e.g., `some substring in the document`).
            query_type (str): One of the following search mode of document deletion in ["fulltext", "AND", "OR"].
            index_name (str, optional): Name of storage index for knowledge base (Default: `self.index_texts`).
        """
        if index_name is None:
            index_name = self.index_texts

        if query_schema_list is None:
            query_schema_list = ["info"]

        self.storage.delete_data(
            table_name=index_name,
            query_schema_list=query_schema_list,
            query_text_list=query_text_list,
            query_type=query_type,
        )

    def process_markdown(
        self, markdown_path: str, sep: list[str] | None = None, max_length: int = 5000, overlap: int = 100
    ) -> None:
        """
        Process a markdown by chunking and embedding, and save its content into storage.

        Args:
            markdown_path (str): Path to a markdown file to be chunked.
            sep (list[str]): List of separator to split the document, leftmost element being the first separator to be
                applied (Default: `["####", "##"]`).
            max_length (int): Maximum number of characters in each split (Default: `5000`).
            overlap (int): Number of characters in the overlap (Default: `100`).
        """
        sep = sep or ["####", "##"]
        markdown_chunks = chunk_markdown(filepath=markdown_path, sep=sep, max_length=max_length, overlap=overlap)
        for chunk in markdown_chunks:
            self.storage.insert_data(
                table_name=self.index_texts,
                data={"info": chunk, "info_embedding": self._embed(chunk), "path": markdown_path},
            )

    def process_user_query(self, user_prompt: str, filepath: str) -> None:
        """
        Extract information from user provided instruction and file, and save into storage.

        Args:
            user_prompt (str): User-provided instruction about how to extract knowledge.
            filepath (str): User-provided file path from which knowledge will be extracted.
        """
        with open(filepath, encoding="utf-8") as file:
            file_content = file.read()

        state = {"user_prompt": user_prompt, "file_content": file_content}
        message = PromptTemplate.from_package_relative(
            f"{PROMPT_MD_PREFIX}/knowledge_base/user_guide_kb"
        ).apply_prompt_template(**state)
        output = model_inference(message)
        self.insert_document(document={"info": output, "info_embedding": self._embed(output), "path": filepath})

    def query_document(
        self,
        index_name: str | None = None,
        mode: str = "fulltext",
        query_schema: str = "info",
        query_text: str = "",
        topk: int = 1,
    ) -> list[dict]:
        """
        Query all documents by its content. Two modes (fulltext and vector similarity search) are supported.

        Args:
            index_name (str, optional): Name of storage index for knowledge base (Default: `self.index_texts`).
            mode (str): One of the following search mode of document query in ["fulltext", "vector"]
                (Default: `fulltext`).
            query_schema (str): Schema of knowledge base table to be searched (Default: `info`).
            query_text (str): User query text (Default: `""`).
            topk (int): Number of documents to be returned (Default: `1`).

        Returns:
            List[dict], retrieved documents.
        """
        if mode not in ["fulltext", "vector"]:
            raise ValueError("Input argument mode must be either 'fulltext' or 'vector'.")

        if index_name is None:
            index_name = self.index_texts

        if mode == "fulltext":
            out = self.storage.query_fulltext(
                table_name=index_name, query_schema=query_schema, query_text=query_text, topk=topk
            )
        else:
            out = self.storage.query_vector(
                table_name=index_name,
                query_schema=query_schema + "_embedding",
                query_vector=list(self._embed(query_text)),
                topk=topk,
            )

        return [{"doc_id": index_name, "doc_content": i} for i in out]

    def query_unified_semantics(self, query_schema: str = "knowledge", query_text: str = "") -> list[dict]:
        """
        Query unified semantics between knowledge graph nodes and metadata graph nodes.

        Args:
            query_schema (str): Whether to search for knowledge or metadata. Supported inputs are "knowledge" and
                "metadata" (Default: `knowledge`).
            query_text (str): User query text (Default: `""`).

        Returns:
            List[dict], retrieved unified semantics mapping.
        """
        if query_schema not in ["knowledge", "metadata"]:
            raise ValueError(
                f'Unsupported query_schema {query_schema}. Supported query_schema are "knowledge" and "metadata".'
            )

        out = self.storage.query_exact(
            table_name=self.index_unified_semantics,
            query_schema_list=[query_schema],
            query_text_list=[query_text],
            query_type="AND",
            topk=1,
        )
        return [{"mapping_id": self.index_unified_semantics, "mapping_content": i} for i in out]

    def _embed(self, query: str | list[str]):
        """Embed text using per-Agent embedding model when configured."""
        return embedding(query, embedding_model=self._embedding_model)
