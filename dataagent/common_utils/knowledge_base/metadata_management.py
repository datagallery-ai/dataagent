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
import random
import re

import numpy as np
import pandas as pd
from loguru import logger

from dataagent.common_utils.knowledge_base.utils_common import (
    StorageConnectorElasticSearch,
    StorageConnectorGaussVector,
)
from dataagent.common_utils.knowledge_base.utils_inference import cosine_similarity, embedding
from dataagent.common_utils.knowledge_base.utils_metadata import (
    create_table_schema_template,
    extract_information,
    infer_data_type,
    infer_file_description,
    infer_joinable_relationship,
    infer_schema_description,
)

# Define schemas of metadata graph nodes
# -- id: metadata graph node id
# -- type: metadata graph node type
# -- label: metadata graph node name
# -- label_embedding: 1024D vector embedding of node name
# -- description: metadata graph node description
# -- description_embedding: 1024D vector embedding of node description
# -- data_type: metadata graph node data type
# -- path: path of data file
MAPPING_GRAPH_NODES = {
    "mappings": {
        "properties": {
            "id": {"type": "integer"},
            "type": {"type": "keyword"},
            "label": {"type": "keyword"},
            "label_embedding": {"type": "dense_vector", "dims": 1024},
            "description": {"type": "keyword"},
            "description_embedding": {"type": "dense_vector", "dims": 1024},
            "data_type": {"type": "keyword"},
            "path": {"type": "keyword"},
        }
    }
}

# Define schemas of metadata graph edges
# -- source: metadata graph edge source
# -- target: metadata graph edge destination
# -- relationship: metadata graph edge type
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


class MetadataManagement:
    """
    Main class of table metadata management module.
    """

    def __init__(
        self,
        hostaddress: str = "",
        storage_type: str | None = None,
        index: str = "default_meta",
        mapping_node: dict | None = None,
        mapping_edge: dict | None = None,
        embedding_model: str | None = None,
    ) -> None:
        """
        Initialize MetadataManagement class and and touch storage service.

        Args:
            hostaddress (str, optional): Host address of storage service (Default: `MEMORY.long_term_storage.url`).
            storage_type (str): Type of storage service (Default: `MEMORY.long_term_storage.backend`).
            index (str): Name of elasticsearch index for metadata (Default: `default_meta`).
            mapping_node (dict, optional): Schema of metadata node tables (Default: None).
            mapping_edge (dict, optional): Schema of metadata edge tables (Default: None).
            embedding_model: MEMORY.embedding_model from per-Agent config (Agent runtime paths).
        """
        self._embedding_model = embedding_model
        mapping_edge = mapping_edge or MAPPING_GRAPH_EDGES
        mapping_node = mapping_node or MAPPING_GRAPH_NODES
        if not hostaddress or not storage_type:
            raise ValueError(
                "MetadataManagement requires explicit hostaddress and storage_type from per-Agent MEMORY config."
            )

        self.hostaddress = hostaddress
        self.index = index
        self.index_nodes = index + "_nodes"
        self.index_edges = index + "_edges"
        self.mapping_nodes = mapping_node
        self.mapping_edges = mapping_edge
        if storage_type == "elasticsearch":
            self.storage = StorageConnectorElasticSearch(hosts=hostaddress)
        elif storage_type == "gaussvector":
            self.storage = StorageConnectorGaussVector(hosts=hostaddress)
        else:
            raise ValueError(
                f'Unsupported storage type {storage_type}. Supported connectors are ["elasticsearch", "gaussvector"].'
            )

        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        self.storage.create_table(table_name=self.index_edges, mapping=self.mapping_edges)
        self.pull_table_metadata()

    @staticmethod
    def infer_file_description_when_register_table(
        df: pd.DataFrame,
        table_path: str,
        knowledge: str,
        provided_meta: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        Infer file description.

        Args:
            df (pd.DataFrame): Dataframe of the table.
            table_path (str): Full path of a table.
            knowledge (str): Related knowledge text chunks.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `{}`).
            metadata (dict): Metadata dictionary to be updated (Default: `None`).
        """
        provided_meta = provided_meta or {}
        metadata = metadata or {}
        # infer file description
        if provided_meta.get("file_description"):
            metadata[table_path]["file_description"] = provided_meta["file_description"]
        else:
            metadata[table_path]["file_description"] = infer_file_description(
                filename=table_path.split("/")[-1],
                document=knowledge,
                columns=list(df.columns),
                file_description=metadata[table_path]["file_description"],
            )

    @staticmethod
    def get_input_columns_info_for_infer_column_data_type(
        df: pd.DataFrame, table_path: str, provided_meta: dict | None = None, metadata: dict | None = None
    ) -> dict:
        """
        Get input columns info for infer column data type.

        Args:
            df (pd.DataFrame): Dataframe of the table.
            table_path (str): Full path of a table.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `{}`).
            metadata (dict): Metadata dictionary to be updated (Default: `None`).

        Returns:
            Dict, input columns info for infer column data type.
        """
        provided_meta = provided_meta or {}
        metadata = metadata or {}
        input_columns_info = {}
        for i in list(df.columns):
            if (
                provided_meta.get("schema")
                and provided_meta["schema"].get(i)
                and provided_meta["schema"][i].get("data_type")
            ):
                metadata[table_path]["schema"][i]["data_type"] = provided_meta["schema"][i]["data_type"]
            else:
                str_values = []
                for v in df[i].dropna():
                    if isinstance(v, dict):
                        str_values.append(json.dumps(v, ensure_ascii=False))
                    else:
                        str_values.append(str(v))
                str_values = list(set(str_values))
                input_columns_info[i] = {}
                input_columns_info[i]["description"] = metadata[table_path]["schema"][i]["schema_description"]
                input_columns_info[i]["sampled_values"] = random.sample(str_values, min(20, len(str_values)))
        return input_columns_info

    @staticmethod
    def get_input_columns_info_for_infer_column_description(
        df: pd.DataFrame, table_path: str, provided_meta: dict | None = None, metadata: dict | None = None
    ) -> dict:
        """
        Get input columns info for infer column description.

        Args:
            df (pd.DataFrame): Dataframe of the table.
            table_path (str): Full path of a table.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `{}`).
            metadata (dict): Metadata dictionary to be updated (Default: `None`).

        Returns:
            Dict, input columns info for infer column description.
        """
        provided_meta = provided_meta or {}
        metadata = metadata or {}
        input_columns_info = {}
        for i in list(df.columns):
            if (
                provided_meta.get("schema")
                and provided_meta["schema"].get(i)
                and provided_meta["schema"][i].get("schema_description")
            ):
                metadata[table_path]["schema"][i]["schema_description"] = provided_meta["schema"][i][
                    "schema_description"
                ]
            else:
                input_columns_info[i] = {}
                str_values = []
                for v in df[i].dropna():
                    if isinstance(v, dict):
                        str_values.append(json.dumps(v, ensure_ascii=False))
                    else:
                        str_values.append(str(v))
                str_values = list(set(str_values))
                input_columns_info[i]["description"] = ""
                input_columns_info[i]["sampled_values"] = random.sample(str_values, min(20, len(str_values)))
        return input_columns_info

    @staticmethod
    def infer_column_data_type(
        table_path: str,
        df: pd.DataFrame,
        provided_meta: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        Infer column data type.

        Args:
            table_path (str): Full path of a table.
            df (pd.DataFrame): Dataframe of the table.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `{}`).
            metadata (dict): Metadata dictionary to be updated (Default: `None`).
        """
        # infer column data type
        input_columns_info = {}
        metadata = metadata or {}
        input_columns_info = MetadataManagement.get_input_columns_info_for_infer_column_data_type(
            df=df, table_path=table_path, provided_meta=provided_meta, metadata=metadata
        )
        if input_columns_info:
            retry, cnt = True, 0
            nretry = 5
            while retry and cnt < nretry:
                retry = False
                infer_type_result = None
                data_dict_type = None
                try:
                    infer_type_result = infer_data_type(columns_info=input_columns_info)
                    try:
                        data_dict_type = json.loads(infer_type_result)
                    except json.JSONDecodeError:
                        logger.debug("Direct JSON parsing failed, trying to extract JSON from text using regex.")
                    if data_dict_type is None:
                        match = re.search(r"```json\s*(.*?)\s*```", infer_type_result, re.DOTALL)
                        if match:
                            match_type = match.group(1)
                            retry = False
                            data_dict_type = json.loads(match_type)
                    if data_dict_type:
                        for key, val in data_dict_type.items():
                            metadata[table_path]["schema"][key]["data_type"] = val
                    else:
                        raise ValueError("Failed to parse column data type inference result into JSON.")
                except Exception as e:
                    logger.warning(f"Infer column data type failed: {e}, retrying...")
                    retry = True
                    cnt += 1
            if cnt == nretry and retry:
                raise ValueError("Failed to infer data type after 5 retries.")

    @staticmethod
    def infer_column_description(
        df: pd.DataFrame,
        table_path: str,
        knowledge: str,
        provided_meta: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        Infer column description.

        Args:
            df (pd.DataFrame): Dataframe of the table.
            table_path (str): Full path of a table.
            knowledge (str): Related knowledge text chunks.
            provided_meta (dict): User provided metadata to be auto-filled into metadata (Default: `{}`).
            metadata (dict): Metadata dictionary to be updated (Default: `None`).
        """
        metadata = metadata or {}
        input_columns_info = MetadataManagement.get_input_columns_info_for_infer_column_description(
            df=df, table_path=table_path, provided_meta=provided_meta, metadata=metadata
        )

        if input_columns_info:
            retry, cnt = True, 0
            nretry = 5
            while retry and cnt < nretry:
                retry = False
                description_result = None
                data_dict_description = None
                try:
                    description_result = infer_schema_description(knowledge=knowledge, columns_info=input_columns_info)
                    # 1. 直接尝试将整个文本解析为 JSON
                    try:
                        data_dict_description = json.loads(description_result)
                    except json.JSONDecodeError:
                        logger.debug("Direct JSON parsing failed, trying to extract JSON from text using regex.")
                    if data_dict_description is None:
                        match = re.search(
                            r"```json\s*(.*?)\s*```",
                            description_result,
                            re.DOTALL,
                        )
                        if match:
                            data_dict_description = json.loads(match.group(1))
                    if data_dict_description:
                        for key, val in data_dict_description.items():
                            metadata[table_path]["schema"][key]["schema_description"] = val
                    else:
                        raise ValueError("Failed to parse column description inference result into JSON.")
                except Exception as e:
                    logger.warning(f"Infer column description failed: {e}, retrying...")
                    retry = True
                    cnt += 1
            if cnt == nretry and retry:
                raise ValueError("Failed to infer column description after 5 retries.")

    def filter_results_by_similarity(
        self,
        mode: str,
        index_name: str,
        query_schema: str,
        query_text: str,
        similarity_threshold: float,
        query_type: str,
        topk: int,
    ) -> list[dict]:
        """
        Filter query results by similarity threshold.

        Args:
            mode (str): One of the following search mode of document query in ("fulltext", "vector").
            index_name (str): Name of storage index.
            query_schema (str): Schema of knowledge base table to be searched.
            query_text (str): User query text.
            similarity_threshold (float): Cosine similarity threshold between column descriptions, only used when
                mode is "vector".
            topk (int): Number of columns to be returned.

        Returns:
            List[dict], filtered query results.
        """
        if mode == "vector":
            vector_embedding = list(self._embed(query_text))
            results = self.storage.query_metadata_column_vector(
                table_name=index_name,
                query_schema=query_schema + "_embedding",
                query_vector=vector_embedding,
                query_type=query_type,
                topk=topk,
            )
            filtered_results = []
            for result in results:
                if result["type"] == query_type:
                    doc_vector = np.array(result[query_schema + "_embedding"], dtype=np.float32)
                    similarity = cosine_similarity(np.array([vector_embedding]), np.array([doc_vector]))[0][0]
                    if similarity >= similarity_threshold:
                        result["_similarity"] = similarity
                        filtered_results.append(result)

                    if len(filtered_results) >= topk:
                        break
        else:
            filtered_results = self.storage.query_metadata_column_fulltext(
                table_name=index_name,
                query_schema=query_schema,
                query_text=query_text,
                query_type=query_type,
                topk=topk,
            )
        return filtered_results

    def build_metadata_info(self, index_name: str, info: dict) -> dict:
        """
        Build metadata information dict.

        Args:
            index_name (str): Name of storage index.
            info (dict): Column information dict.

        Returns:
            Dict, metadata information dict.
        """
        ret = {
            "metadata_id": index_name,
            "metadata_content": {
                "id": info["id"],
                "type": info["type"],
                "path": info["path"],
                "data_type": info["data_type"],
                "label": info["label"],
                "description": info["description"],
                "relationship": self.metadata[info["path"]]["schema"][info["label"]]["relationship"],
                "potential_relationship": self.metadata[info["path"]]["schema"][info["label"]][
                    "potential_relationship"
                ],
            },
        }
        return ret

    def export_metadata(self, filepath: str) -> None:
        """
        Export metadata dictionary into a json file.

        Args:
            filepath (str): Output path of json file.
        """
        out = []
        for i, j in self.metadata.items():
            out.append({i: j})

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=4, ensure_ascii=False)

    def get_column_metadata_exactly(self, filepath: str, column_name: str, index_name: str | None = None) -> dict:
        """
        Retrieve column metadata by exact filepath and column name.

        Args:
            filepath (str): Path of the file.
            column_name (str): Name of the column.
            index_name (str, optional): Name of the storage index to search in (Default: `self.index_nodes`).

        Returns:
            Dict, metadata of the column, or empty dict if not found.
        """
        if index_name is None:
            index_name = self.index_nodes

        results = self.metadata.get(filepath, {}).get("schema", {}).get(column_name, None)
        if not results:
            return {}

        out = {
            "metadata_id": index_name,
            "metadata_content": {
                "filepath": filepath,
                "column_name": column_name,
                "column_description": results["schema_description"],
                "column_data_type": results["data_type"],
                "column_relationship": self.metadata[filepath]["schema"][column_name]["relationship"],
                "column_potential_relationship": (
                    self.metadata[filepath]["schema"][column_name]["potential_relationship"]
                ),
            },
        }
        return out

    def pull_table_metadata(self) -> None:
        """
        Pull table metadata information from storage to local class attributes.
        """
        self.metadata = {}
        self.registered_file = set()
        nodes = self.storage.query_all(table_name=self.index_nodes)
        relation_edges = self.storage.query_metadata_edge(table_name=self.index_edges, edge_type="relationship")
        potential_edges = self.storage.query_metadata_edge(
            table_name=self.index_edges, edge_type="potential_relationship"
        )
        id_to_name = {i["id"]: i["path"] + " -> " + i["label"] for i in nodes}
        for i in nodes:
            if i["path"] not in self.metadata:
                self.metadata[i["path"]] = {"id": 0, "file_description": "", "schema": {}}

            if i["type"] == "file":
                self.registered_file.add(i["label"])
                self.metadata[i["path"]]["id"] = i["id"]
                self.metadata[i["path"]]["file_description"] = i["description"]
            elif i["type"] == "column":
                self.metadata[i["path"]]["schema"][i["label"]] = {
                    "id": i["id"],
                    "schema_description": i["description"],
                    "data_type": i["data_type"],
                    "relationship": [],
                    "potential_relationship": [],
                }
            else:
                raise ValueError(f"Unknown node type '{i['type']}' in metadata graph.")

        for i in relation_edges:
            source = id_to_name[i["source"]].split(" -> ")
            target = id_to_name[i["target"]]
            self.metadata[source[0]]["schema"][source[1]]["relationship"].append(
                {"label": target, "type": i["relationship"]}
            )

        for i in potential_edges:
            source = id_to_name[i["source"]].split(" -> ")
            target = id_to_name[i["target"]]
            self.metadata[source[0]]["schema"][source[1]]["potential_relationship"].append(
                {"label": target, "type": i["potential_relationship"]}
            )

    def get_node_and_edge_info(
        self,
    ) -> tuple[list[dict], list[dict]]:
        """
        Get node and edge information from local metadata dict.

        Returns:
            Tuple[List[dict], List[dict]], node information and edge information.
        """
        node_info = []
        edge_info = []
        for i, j in self.metadata.items():
            file_info = {
                "id": j["id"],
                "type": "file",
                "label": i,
                "label_embedding": self._embed(i),
                "description": j["file_description"],
                "description_embedding": self._embed(j["file_description"]),
                "data_type": "file",
                "path": i,
            }
            node_info.append(file_info)
            for col, vals in j["schema"].items():
                column_info = {
                    "id": vals["id"],
                    "type": "column",
                    "label": col,
                    "label_embedding": self._embed(col),
                    "description": vals["schema_description"],
                    "description_embedding": self._embed(vals["schema_description"]),
                    "data_type": vals["data_type"],
                    "path": i,
                }
                node_info.append(column_info)
                edge_info.append({"source": j["id"], "target": vals["id"], "relationship": "has_column"})
                relation_map = [
                    ("relationship", vals["relationship"]),
                    ("potential_relationship", vals["potential_relationship"]),
                ]
                for relation_type, relations in relation_map:
                    for relation in relations:
                        target = relation["label"].split(" -> ")
                        relation_info = {
                            "source": vals["id"],
                            "target": self.metadata[target[0]]["schema"][target[1]]["id"],
                            relation_type: relation["type"],
                        }
                        edge_info.append(relation_info)
        return node_info, edge_info

    def push_table_metadata(self) -> None:
        """
        Push local table metadata dict into storage.
        """
        self.storage.drop_table(self.index_nodes)
        self.storage.drop_table(self.index_edges)
        self.storage.create_table(table_name=self.index_nodes, mapping=self.mapping_nodes)
        self.storage.create_table(table_name=self.index_edges, mapping=self.mapping_edges)

        node_info, edge_info = self.get_node_and_edge_info()
        for node in node_info:
            self.storage.insert_data(table_name=self.index_nodes, data=node)

        for edge in edge_info:
            self.storage.insert_data(table_name=self.index_edges, data=edge)

    def query_all_columns_by_filepath(self, filepath: str, index_name: str | None = None) -> list[dict]:
        """
        Query all columns from a table by table path.

        Args:
            filepath (str): User query text.
            index_name (str, optional): Name of storage index (Default: `self.index_nodes`).

        Returns:
            List[dict], retrieved columns in the format of [{"metadata_id":"...", "metadata_content":"...", ...]
        """
        if index_name is None:
            index_name = self.index_nodes

        out = []
        results = self.storage.query_metadata_columns_by_filepath(table_name=index_name, filepath=filepath)
        for i in results:
            out.append(self.build_metadata_info(index_name=index_name, info=i))

        return out

    def query_column_metadata(
        self,
        index_name: str | None = None,
        mode: str = "fulltext",
        query_schema: str = "label",
        query_text: str = "",
        similarity_threshold: float = 0.5,
        topk: int = 1,
    ) -> list[dict]:
        """
        Query column information by column name or description.

        Args:
            index_name (str, optional): Name of storage index (Default: `self.index_nodes`).
            mode (str): One of the following search mode of document query in ("fulltext", "vector")
                (Default: `fulltext`).
            query_schema (str): Schema of knowledge base table to be searched, one of the following schema
                ("label", "description") (Default: `label`).
            query_text (str): User query text (Default: `""`).
            similarity_threshold (float): Cosine similarity threshold between column descriptions, only used when
                mode is "vector" (Default: `0.5`).
            topk (int): Number of columns to be returned (Default: `1`).

        Returns:
            List[dict], retrieved columns in the format of [{"metadata_id":"...", "metadata_content":"...", ...]
        """
        if mode not in ["fulltext", "vector"]:
            raise ValueError("Input argument mode must be either 'fulltext' or 'vector'.")

        if query_schema not in ["label", "description"]:
            raise ValueError("Input argument query_schema must be either 'label' or 'description'.")

        if index_name is None:
            index_name = self.index_nodes

        out = []
        filtered_results = self.filter_results_by_similarity(
            mode=mode,
            index_name=index_name,
            query_schema=query_schema,
            query_text=query_text,
            similarity_threshold=similarity_threshold,
            query_type="column",
            topk=topk,
        )

        for i in filtered_results:
            out.append(self.build_metadata_info(index_name=index_name, info=i))

        return out

    def query_table_metadata(
        self,
        index_name: str | None = None,
        mode: str = "fulltext",
        query_schema: str = "description",
        query_text: str = "",
        similarity_threshold: float = 0.5,
        topk: int = 1,
    ) -> list[dict]:
        """
        Query table information by table file path or table description.

        Args:
            index_name (str, optional): Name of storage index (Default: `self.index_nodes`).
            mode (str): One of the following search mode of document query in ("fulltext", "vector")
                (Default: `fulltext`).
            query_schema (str): Schema of knowledge base table to be searched one of the following schema
                ("label", "description") (Default: `column_name`).
            query_text (str): User query text (Default: `""`).
            similarity_threshold (float): Cosine similarity threshold between column descriptions, only used when
                mode is "vector" (Default: `0.5`).
            topk (int): Number of columns to be returned (Default: `1`).

        Returns:
            List[dict], retrieved documents in the format of [{"metadata_id":"...", "metadata_content":"..."}, ...]
        """
        if mode not in ["fulltext", "vector"]:
            raise ValueError("Input argument mode must be either 'fulltext' or 'vector'.")

        if query_schema not in ["label", "description"]:
            raise ValueError("Input argument query_schema must be either 'label' or 'description'.")

        if index_name is None:
            index_name = self.index_nodes

        out = []
        filtered_results = self.filter_results_by_similarity(
            mode=mode,
            index_name=index_name,
            query_schema=query_schema,
            query_text=query_text,
            similarity_threshold=similarity_threshold,
            query_type="file",
            topk=topk,
        )
        for i in filtered_results:
            out.append(
                {
                    "metadata_id": index_name,
                    "metadata_content": {
                        "id": i["id"],
                        "type": i["type"],
                        "path": i["path"],
                        "label": i["label"],
                        "description": i["description"],
                    },
                }
            )

        return out

    def infer_joinable_relationship_when_register_table(
        self, metadata: dict | None = None, enable_relationship_inference: bool = True
    ) -> None:
        """
        Infer joinable relationships between columns in different tables.

        Args:
            metadata (dict | None): Metadata dictionary to be updated (Default: `None`).
            enable_relationship_inference (bool): Whether to enable joinable column inference (Default: `True`).
        """
        metadata = metadata or {}
        if enable_relationship_inference:
            columns_ids, columns_descriptions, columns_values = extract_information(index=self.index, metadata=metadata)
            if self.metadata:
                old_columns_ids, old_columns_descriptions, old_columns_values = extract_information(
                    index=self.index, metadata=self.metadata
                )
                joinable_pairs = infer_joinable_relationship(
                    col_ids1=old_columns_ids,
                    col_ids2=columns_ids,
                    columns_descriptions1=old_columns_descriptions,
                    columns_descriptions2=columns_descriptions,
                    columns_values1=old_columns_values,
                    columns_values2=columns_values,
                    embedding_model=self._embedding_model,
                )
                for i, j in joinable_pairs:
                    old = i.split(" -> ")
                    new = j.split(" -> ")
                    self.metadata[old[0]]["schema"][old[1]]["relationship"].append(
                        {"label": j, "type": "is_joinable_with"}
                    )

                    metadata[new[0]]["schema"][new[1]]["relationship"].append({"label": i, "type": "is_joinable_with"})

    def register_table(
        self,
        table_path: str,
        df_knowledge: tuple[pd.DataFrame, list[str]],
        provided_meta: dict | None = None,
        file_type: str = "localfile",
        enable_relationship_inference: bool = True,
    ) -> None:
        """
        Register a table and extract all relevant information, including column descriptions, data types and
            joinable columns.

        Args:
            table_path (str): Full path of a table.
            df_knowledge (tuple[pd.DataFrame, list[str]]): Tuple of table to be registered, and list of
                related knowledge text chunks.
            provided_meta (str | None): User provided metadata to be auto-filled into metadata (Default: `{}`).
            file_type (str): Type of data source (Default: `"localfile"`).
            enable_relationship_inference (bool): Whether to enable joinable column inference (Default: `True`).
        """
        provided_meta = provided_meta or {}
        if table_path in self.registered_file:
            return

        knowledge = "\n".join(df_knowledge[1])
        if self.metadata:
            max_id = max([v["id"] for _, j in self.metadata.items() for _, v in j["schema"].items()])
        else:
            max_id = -1

        metadata = create_table_schema_template(
            filename=table_path, columns=list(df_knowledge[0].columns), startnum=max_id + 1
        )

        # infer file description
        MetadataManagement.infer_file_description_when_register_table(
            df=df_knowledge[0],
            table_path=table_path,
            knowledge=knowledge,
            provided_meta=provided_meta,
            metadata=metadata,
        )

        # infer column description
        MetadataManagement.infer_column_description(
            df=df_knowledge[0],
            table_path=table_path,
            knowledge=knowledge,
            provided_meta=provided_meta,
            metadata=metadata,
        )

        # infer column data type
        MetadataManagement.infer_column_data_type(
            table_path=table_path, df=df_knowledge[0], provided_meta=provided_meta, metadata=metadata
        )

        # infer joinable relationships
        self.infer_joinable_relationship_when_register_table(
            metadata=metadata, enable_relationship_inference=enable_relationship_inference
        )

        # provide_metadata is not empty
        if provided_meta.get("schema"):
            for cur_schema, cur_schema_val in provided_meta["schema"].items():
                if (
                    cur_schema_val.get("relationship")
                    and cur_schema_val["relationship"] not in metadata[table_path]["schema"][cur_schema]["relationship"]
                ):
                    for cur_add_schema in cur_schema_val["relationship"]:
                        metadata[table_path]["schema"][cur_schema]["relationship"].append(cur_add_schema)

                if (
                    cur_schema_val.get("potential_relationship")
                    and cur_schema_val["potential_relationship"]
                    not in metadata[table_path]["schema"][cur_schema]["potential_relationship"]
                ):
                    for cur_add_schema in cur_schema_val["potential_relationship"]:
                        metadata[table_path]["schema"][cur_schema]["potential_relationship"].append(cur_add_schema)

        # update self.metadata
        self.metadata.update(metadata)

        # register file
        self.registered_file.add(table_path)

    def remove_table(self, table_path: str) -> None:
        """
        Remove table metadata information of a table.

        Args:
            table_path (str): Full file path of a table.
        """
        if table_path not in self.metadata:
            return

        columnids = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["path"],
            query_text_list=[table_path],
            query_type="AND",
            topk=10000,
        )
        if self.storage.query_all(self.index_edges):
            for cur_columnids in columnids:
                self.storage.delete_data(
                    table_name=self.index_edges,
                    query_schema_list=["source", "target"],
                    query_text_list=[cur_columnids["id"], cur_columnids["id"]],
                    query_type="OR",
                )

        self.storage.delete_data(
            table_name=self.index_nodes, query_schema_list=["path"], query_text_list=[table_path], query_type="AND"
        )
        self.pull_table_metadata()

    def update_metadata(
        self, metadata_label: str, metadata_file_path: str, update_field: str, update_value: str
    ) -> None:
        """
        Update metadata information into storage service.

        Args:
            metadata_label (str): The label of the metadata is used to identify which metadata data to modify.
            metadata_file_path (str): The file path of the metadata is used to identify which metadata data to modify.
            update_field (str): Fields that need to be updated in the metadata.
            update_value (str): The value of metadata to be updated.
        """
        if update_field not in ["description", "data_type"]:
            raise ValueError("Input argument query_schema must be either 'description' or 'data_type'.")

        select_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[metadata_label, metadata_file_path],
            query_type="AND",
            topk=1,
        )[0]
        if not select_node:
            raise ValueError(f"Metadata Label '{metadata_label}' is not found in the node metadata.")

        self.storage.update_value(
            table_name=self.index_nodes,
            pos_query=["label", "path"],
            pos_text=[metadata_label, metadata_file_path],
            update_field=update_field,
            update_value=update_value,
        )
        if update_field == "description":
            if select_node["type"] == "file":
                self.metadata[select_node["path"]][f"file_{update_field}"] = update_value
            elif select_node["type"] == "column":
                self.metadata[select_node["path"]]["schema"][select_node["label"]][f"schema_{update_field}"] = (
                    update_value
                )
        elif update_field == "data_type":
            self.metadata[select_node["path"]]["schema"][select_node["label"]][update_field] = update_value

    def add_metadata_node(
        self,
        node_label: str,
        node_type: str,
        node_description: str,
        node_data_type: str,
        node_path: str,
        node_path_description: str = "",
    ) -> None:
        """
        Add a node for metadata into storage service.

        Args:
            node_label (str): Name of the new node.
            node_type (str): Type of the new node, one of the following ('file', 'column').
            node_description (str): Description of the new node.
            node_data_type (str): Data type of the new node.
            node_path (str): File path of the new node.
            node_path_description (str): Description of the file of the new node. Only used when the new node does not
                belong to any of the existing files (Default: `""`).
        """
        if node_type not in ["file", "column"]:
            raise ValueError("Input argument node_type must be either 'file' or 'column'.")

        addnode_doc = {
            "id": max([int(v["id"]) for _, j in self.metadata.items() for _, v in j["schema"].items()]) + 1,
            "type": node_type,
            "label": node_label,
            "label_embedding": self._embed(node_label),
            "description": node_description,
            "description_embedding": self._embed(node_description),
            "data_type": node_data_type,
            "path": node_path,
        }
        self.storage.insert_data(table_name=self.index_nodes, data=addnode_doc)
        if node_path not in self.metadata:
            self.metadata[node_path] = {"id": 0, "file_description": node_path_description, "schema": {}}

        if node_type == "file":
            self.registered_file.add(node_label)
            self.metadata[node_path]["id"] = addnode_doc["id"]
            self.metadata[node_path]["file_description"] = node_description
        else:
            self.metadata[node_path]["schema"][node_label] = {
                "id": addnode_doc["id"],
                "schema_description": node_description,
                "data_type": node_data_type,
                "relationship": [],
                "potential_relationship": [],
            }

    def delete_metadata_node(self, node_label: str, node_path: str) -> None:
        """
        Delete a node for metadata from storage service.

        Args:
            node_label (str): Name of the node to be deleted.
            node_path(str): File path of the node to be deleted.
        """
        select_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[node_label, node_path],
            query_type="AND",
            topk=1,
        )[0]
        if select_node is None:
            raise ValueError("Input Metadata is not found in the node metadata.")

        self.storage.delete_data(
            table_name=self.index_nodes, query_schema_list=["id"], query_text_list=[select_node["id"]], query_type="AND"
        )
        self.storage.delete_data(
            table_name=self.index_edges,
            query_schema_list=["source", "target"],
            query_text_list=[select_node["id"], select_node["id"]],
            query_type="OR",
        )
        if select_node["type"] == "file":
            del self.metadata[select_node["path"]]
            self.registered_file.remove(node_label)
        elif select_node["type"] == "column":
            del self.metadata[select_node["path"]]["schema"][node_label]
            for _, curMetadata in self.metadata.items():
                for _, curSchemaValue in curMetadata["schema"].items():
                    relationship_list = curSchemaValue["relationship"]
                    potiential_relationship_list = curSchemaValue["potential_relationship"]
                    for relationship in relationship_list:
                        if select_node["label"] in relationship["label"]:
                            relationship_list.remove(relationship)

                    for potential_relationship in potiential_relationship_list:
                        if select_node["label"] in potential_relationship["label"]:
                            potiential_relationship_list.remove(potential_relationship)

    def add_relationship(
        self,
        source_label: str,
        source_filepath: str,
        target_label: str,
        target_filepath: str,
        relationship_type: str,
        relationship_description: str,
    ) -> None:
        """
        Add relationship between two nodes for metadata into storage service.

        Args:
            source_label (str): Label of the source node.
            source_filepath (str): File path of the source node.
            target_label (str): Label of the target node.
            target_filepath (str): File path of the target node.
            relationship_type (str): Type of relationship, one of the following
                ('relationship', 'potential_relationship').
            relationship (str): Description of the relationship between two nodes.
        """
        if relationship_type not in ["relationship", "potential_relationship"]:
            raise ValueError(
                "Input argument relationship_type must be either 'relationship' or 'potential_relationship'."
            )

        source_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[source_label, source_filepath],
            query_type="AND",
            topk=1,
        )
        target_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[target_label, target_filepath],
            query_type="AND",
            topk=1,
        )
        if not source_node or not target_node:
            return

        # update local
        source = source_node[0]
        target = target_node[0]
        if relationship_type == "relationship":
            edge_doc = {
                "source": source["id"],
                "target": target["id"],
                "relationship": relationship_description,
                "potential_relationship": "",
            }
            appenddict = {"label": f"{target['path']} -> {target['label']}", "type": relationship_description}
            if appenddict not in self.metadata[source["path"]]["schema"][source["label"]]["relationship"]:
                self.metadata[source["path"]]["schema"][source["label"]]["relationship"].append(
                    {"label": f"{target['path']} -> {target['label']}", "type": relationship_description}
                )
                self.storage.insert_data(table_name=self.index_edges, data=edge_doc)
        else:
            edge_doc = {
                "source": source["id"],
                "target": target["id"],
                "relationship": "",
                "potential_relationship": relationship_description,
            }
            appenddict = {"label": f"{target['path']} -> {target['label']}", "type": relationship_description}
            if appenddict not in self.metadata[source["path"]]["schema"][source["label"]]["potential_relationship"]:
                self.metadata[source["path"]]["schema"][source["label"]]["potential_relationship"].append(
                    {"label": f"{target['path']} -> {target['label']}", "type": relationship_description}
                )
                self.storage.insert_data(table_name=self.index_edges, data=edge_doc)

    def delete_relationship(
        self, source_label: str, source_filepath: str, target_label: str, target_filepath: str
    ) -> None:
        """
        Delete metadata and the relationship between the two nodes in the storage server

        Args:
            source_label (str): Source node label.
            source_filepath (str): Source node file path.
            target_label (str): Target node label.
            target_filepath (str): Target node file path.
        """
        source_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[source_label, source_filepath],
            query_type="AND",
            topk=1,
        )
        target_node = self.storage.query_exact(
            table_name=self.index_nodes,
            query_schema_list=["label", "path"],
            query_text_list=[target_label, target_filepath],
            query_type="AND",
            topk=1,
        )
        if not source_node or not target_node:
            return

        source = source_node[0]
        target = target_node[0]
        self.storage.delete_data(
            table_name=self.index_edges,
            query_schema_list=["source", "target"],
            query_text_list=[source["id"], target["id"]],
            query_type="AND",
        )
        # update local
        relationship_tags = ["relationship", "potential_relationship"]
        for tag in relationship_tags:
            relationships = self.metadata[source["path"]]["schema"][source["label"]][tag]
            label_pattern = f"{target['path']} -> {target['label']}"
            self.metadata[source["path"]]["schema"][source["label"]][tag] = [
                item for item in relationships if item.get("label") != label_pattern
            ]

    def _embed(self, query: str | list[str]):
        """Embed text using per-Agent embedding model when configured."""
        return embedding(query, embedding_model=self._embedding_model)
