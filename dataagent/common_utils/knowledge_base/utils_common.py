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
import abc
import datetime as dt
import re
from typing import Any

import pandas as pd
from elasticsearch import Elasticsearch
from pandas._libs.tslibs.timestamps import Timestamp
from sqlalchemy import create_engine


class BaseStorageConnector(abc.ABC):
    """
    Abstract class for long-term memory storage connectors.
    """

    @abc.abstractmethod
    def create_table(self, table_name: str, mapping: dict) -> None:
        """
        Create table in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table to be created.
            mapping (dict): Header and schema of table to be created.
        """
        pass

    @abc.abstractmethod
    def drop_table(self, table_name: str) -> None:
        """
        Delete table in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table to be deleted.
        """
        pass

    @abc.abstractmethod
    def insert_data(self, table_name: str, data: dict) -> None:
        """
        Insert a row into tables in ElasticSearch.

        Args:
            table_name (str): Name of table into which a row is to be inserted.
            data (str): Row content to be inserted.
        """
        pass

    @abc.abstractmethod
    def delete_data(
        self, table_name: str, query_schema_list: list[str], query_text_list: list[str | int], query_type: str
    ) -> None:
        """
        Delete rows satisfying specific conditions from tables in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table from which rows are to be deleted.
            query_schema_list (list[str]): List of schemas of constraints.
            query_text_list (list[str]): List of query texts.
            query_type (str): Constraints type in the query_schema_list, one of the following ('fulltext', 'AND', 'OR').
        """
        pass

    @abc.abstractmethod
    def query_fulltext(self, table_name: str, query_schema: str, query_text: str, topk: int) -> list[dict]:
        """
        Full text search in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table to be queried.
            query_schema (str): Schema of table to be queried.
            query_text (str): Query text.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        pass

    @abc.abstractmethod
    def query_vector(self, table_name: str, query_schema: str, query_vector: list[float], topk: int) -> list[dict]:
        """
        Vector search in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table to be queried.
            query_schema (str): Schema of table to be queried.
            query_vector (list[float]): Query vector.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        pass

    @abc.abstractmethod
    def query_exact(
        self,
        table_name: str,
        query_schema_list: list[str],
        query_text_list: list[str | int],
        query_type: str,
        topk: int,
    ) -> list[dict]:
        """
        Exact value search in ElasticSearch or GaussVector.

        Args:
            table_name (str): Name of table to be queried.
            query_schema_list (list[str]): List of schemas of constraints.
            query_text_list (list[str]): List of query texts.
            query_type (str): Constraints type in the query_schema_list, one of the following ('AND', 'OR').
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        pass

    @abc.abstractmethod
    def query_metadata_edge(self, table_name: str, edge_type: str) -> list[dict]:
        """
        Query metadata edges. Either relationships excluding 'has_column', or potential relationships.

        Args:
            table_name (str): Name of edge table to be queried.
            edge_type (str): Type of relationships, one of the following ('relationship', 'potential_relationship').

        Returns:
            List[dict], list of edges to be returned.
        """
        pass

    @abc.abstractmethod
    def query_metadata_columns_by_filepath(self, table_name: str, filepath: str) -> list[dict]:
        """
        Query all columns of a table.

        Args:
            table_name (str): Name of node table to be queried.
            filepath (str): Name of table.

        Returns:
            List[dict], list of column information.
        """
        pass

    @abc.abstractmethod
    def query_metadata_column_vector(
        self, table_name: str, query_schema: str, query_vector: list[float], query_type: str, topk: int
    ) -> list[dict]:
        """
        Query columns by vector search.

        Args:
            table_name (str): Name of node table to be queried.
            query_schema (str): Schema of metadata table to be queried.
            query_vector (list[float]): Query vector.
            query_type (str): Constraints on content in 'type' column of metadata table.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of column information.
        """
        pass

    @abc.abstractmethod
    def query_metadata_column_fulltext(
        self, table_name: str, query_schema: str, query_text: str, query_type: str, topk: int
    ) -> list[dict]:
        """
        Query columns by full text search.

        Args:
            table_name (str): Name of node table to be queried.
            query_schema (str): Schema of metadata table to be queried.
            query_vector (str): Query vector.
            query_type (str): Constraints on content in 'type' column of metadata table.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of column information.
        """
        pass

    @abc.abstractmethod
    def query_all(self, table_name: str) -> list[dict]:
        """
        Get full table from ElasticSearch.

        Args:
            table_name (str): Name of table to be returned.

        Returns:
            List[dict], list of row contents.
        """
        pass

    @abc.abstractmethod
    def update_value(
        self, table_name: str, pos_query: list[str], pos_text: list[str], update_field: str, update_value: str
    ) -> None:
        """
        Update row content satisfying specific constraints.

        Args:
            table_name (str): Name of table to be updated.
            pos_query (list[str]): List of schemas of constraints.
            pos_text (list[str]): List of query texts.
            update_field (str): Name of table schema to be updated.
            update_value (str): Schema content to be updated.
        """
        pass

    @abc.abstractmethod
    def query_relationship(self, table_name: str, exists_field: str) -> list[dict]:
        """
        Query table whose one specific schema is not empty.

        Args:
            table_name (str): Name of table to be queried.
            exists_field (str): Name of specific schema which is required to be non-empty.

        Returns:
            List[dict], list of row contents.
        """
        pass


class StorageConnectorElasticSearch(BaseStorageConnector):
    """
    Storage adaptor to ElasticSearch.
    """

    def __init__(self, hosts: str | None = None) -> None:
        """
        Initialize storage adaptor.

        Args:
            hosts (str): Host api of ElasticSearch (Default: `None`).
        """
        self._es_major_version: int | None = None
        self.es = Elasticsearch(hosts=hosts)

    @staticmethod
    def _get_search_hits(resp: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not isinstance(resp, dict):
            return []
        return resp.get("hits", {}).get("hits", []) or []

    def create_table(self, table_name: str, mapping: dict) -> None:
        """
        Create table in ElasticSearch.

        Args:
            table_name (str): Name of table to be created.
            mapping (dict): Header and schema of table to be created.
        """
        if not self.es.indices.exists(index=table_name):
            self.es.indices.create(index=table_name, body=mapping)

    def drop_table(self, table_name: str) -> None:
        """
        Delete table in ElasticSearch.

        Args:
            table_name (str): Name of table to be deleted.
        """
        if self.es.indices.exists(index=table_name):
            self.es.indices.delete(index=table_name)

    def insert_data(self, table_name: str, data: dict) -> None:
        """
        Insert a row into tables in ElasticSearch.

        Args:
            table_name (str): Name of table into which a row is to be inserted.
            data (str): Row content to be inserted.
        """
        self.es.index(index=table_name, body=data, refresh=True)

    def delete_data(
        self, table_name: str, query_schema_list: list[str], query_text_list: list[str | int], query_type: str
    ) -> None:
        """
        Delete rows satisfying specific conditions from tables in ElasticSearch.

        Args:
            table_name (str): Name of table from which rows are to be deleted.
            query_schema_list (list[str]): List of schemas of constraints.
            query_text_list (list[str]): List of query texts.
            query_type (str): Constraints type in the query_schema_list, one of the following ('fulltext', 'AND', 'OR').
        """
        if query_type == "fulltext" and isinstance(query_text_list[0], str):
            body_query = {
                "query": {"bool": {"must": [{"wildcard": {query_schema_list[0]: "*" + query_text_list[0] + "*"}}]}},
                "size": 10000,
            }
        elif query_type == "AND":
            must_list = []
            for schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                must_list.append({"term": {schema: query_text}})
            body_query = {"query": {"bool": {"must": must_list}}, "size": 10000}
        elif query_type == "OR":
            should_list = []
            for schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                should_list.append({"term": {schema: query_text}})
            body_query = {"query": {"bool": {"should": should_list}}, "size": 10000}
        else:
            raise ValueError(f"Unsupported query_type: {query_type}")
        self.es.delete_by_query(index=table_name, body=body_query, refresh=True)

    def query_fulltext(self, table_name: str, query_schema: str, query_text: str, topk: int) -> list[dict]:
        """
        Full text search in ElasticSearch.

        Args:
            table_name (str): Name of table to be queried.
            query_schema (str): Schema of table to be queried.
            query_text (str): Query text.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        query = {"query": {"bool": {"must": [{"wildcard": {query_schema: "*" + query_text + "*"}}]}}, "size": topk}
        return self._search_sources(table_name, query)

    def query_vector(self, table_name: str, query_schema: str, query_vector: list[float], topk: int) -> list[dict]:
        """
        Vector search in ElasticSearch.

        Args:
            table_name (str): Name of table to be queried.
            query_schema (str): Schema of table to be queried.
            query_vector (list[float]): Query vector.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        query = self._build_script_score_query(query_schema, query_vector, topk)
        return self._search_sources(table_name, query)

    def query_exact(
        self,
        table_name: str | None,
        query_schema_list: list[str],
        query_text_list: list[str | int],
        query_type: str,
        topk: int,
    ) -> list[dict]:
        """
        Exact value search in ElasticSearch.

        Args:
            table_name (str | None): Name of table to be queried.
            query_schema_list (list[str]): List of schemas of constraints.
            query_text_list (list[str]): List of query texts.
            query_type (str): Constraints type in the query_schema_list, one of the following ('AND', 'OR').
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of row contents to be returned.
        """
        if query_type == "AND":
            must_list = []
            for schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                must_list.append({"term": {schema: query_text}})
            body_query = {"query": {"bool": {"must": must_list}}, "size": topk}
        elif query_type == "OR":
            should_list = []
            for schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                should_list.append({"term": {schema: query_text}})
            body_query = {"query": {"bool": {"should": should_list}}, "size": topk}
        else:
            raise ValueError(f"Unsupported query_type: {query_type}")
        return self._search_sources(table_name, body_query)

    def query_metadata_edge(self, table_name: str, edge_type: str) -> list[dict]:
        """
        Query metadata edges. Either relationships excluding 'has_column', or potential relationships.

        Args:
            table_name (str): Name of edge table to be queried.
            edge_type (str): Type of relationships, one of the following ('relationship', 'potential_relationship').

        Returns:
            List[dict], list of edges to be returned.
        """
        if edge_type == "relationship":
            query_body = {
                "size": 10000,
                "query": {
                    "bool": {
                        "must": [
                            {"exists": {"field": "relationship"}},
                            {"bool": {"must_not": {"term": {"relationship": "has_column"}}}},
                        ]
                    }
                },
            }
            out_edges = self._search_sources(table_name, query_body)
        elif edge_type == "potential_relationship":
            query_body = {"size": 10000, "query": {"exists": {"field": "potential_relationship"}}}
            out_edges = self._search_sources(table_name, query_body)
        else:
            raise ValueError("Supported edge types are 'relationship' and 'potential_relationship'.")

        return out_edges

    def query_metadata_columns_by_filepath(self, table_name: str, filepath: str) -> list[dict]:
        """
        Query all columns of a table.

        Args:
            table_name (str): Name of node table to be queried.
            filepath (str): Name of table.

        Returns:
            List[dict], list of column information.
        """
        query = {
            "query": {"bool": {"must": [{"term": {"type": "column"}}, {"wildcard": {"path": "*" + filepath + "*"}}]}},
            "size": 10000,
        }
        return self._search_sources(table_name, query)

    def query_metadata_column_vector(
        self, table_name: str, query_schema: str, query_vector: list[float], query_type: str, topk: int
    ) -> list[dict]:
        """
        Query columns by vector search.

        Args:
            table_name (str): Name of node table to be queried.
            query_schema (str): Schema of metadata table to be queried.
            query_vector (list[float]): Query vector.
            query_type (str): Constraints on content in 'type' column of metadata table.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of column information.
        """
        query = self._build_script_score_query(
            query_schema,
            query_vector,
            topk,
            filters=[{"term": {"type": query_type}}],
        )
        return self._search_sources(table_name, query)

    def query_metadata_column_fulltext(
        self, table_name: str, query_schema: str, query_text: str, query_type: str, topk: int
    ) -> list[dict]:
        """
        Query columns by full text search.

        Args:
            table_name (str): Name of node table to be queried.
            query_schema (str): Schema of metadata table to be queried.
            query_vector (str): Query vector.
            query_type (str): Constraints on content in 'type' column of metadata table.
            topk (str): Maximum number of rows to be returned.

        Returns:
            List[dict], list of column information.
        """
        query = {
            "query": {
                "bool": {"must": [{"wildcard": {query_schema: "*" + query_text + "*"}}, {"term": {"type": query_type}}]}
            },
            "size": topk,
        }
        return self._search_sources(table_name, query)

    def query_all(self, table_name: str) -> list[dict]:
        """
        Get full table from ElasticSearch.

        Args:
            table_name (str): Name of table to be returned.

        Returns:
            List[dict], list of row contents.
        """
        query = {"query": {"match_all": {}}, "size": 10000}
        return self._search_sources(table_name, query)

    def update_value(
        self, table_name: str, pos_query: list[str], pos_text: list[str], update_field: str, update_value: str
    ) -> None:
        """
        Update row content satisfying specific constraints.

        Args:
            table_name (str): Name of table to be updated.
            pos_query (list[str]): List of schemas of constraints.
            pos_text (list[str]): List of query texts.
            update_field (str): Name of table schema to be updated.
            update_value (str): Schema content to be updated.
        """
        must_list = []
        for schema, txt in zip(pos_query, pos_text, strict=True):
            must_list.append({"term": {schema: txt}})
        query = {
            "query": {"bool": {"must": must_list}},
            "script": {
                "source": f"""
                    ctx._source.{update_field} = params.{update_field};
                    ctx._source.updated_at = params.current_time
                    """,
                "params": {
                    update_field: update_value,
                    "current_time": pd.Timestamp(dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
        }
        self.es.update_by_query(index=table_name, body=query, refresh=True)

    def query_relationship(self, table_name: str, exists_field: str) -> list[dict]:
        """
        Query table whose one specific schema is not empty.

        Args:
            table_name (str): Name of table to be queried.
            exists_field (str): Name of specific schema which is required to be non-empty.

        Returns:
            List[dict], list of row contents.
        """
        query_body = {"size": 10000, "query": {"exists": {"field": exists_field}}}
        return self._search_sources(table_name, query_body)

    def _get_es_major_version(self) -> int | None:
        cached_version = self._es_major_version if hasattr(self, "_es_major_version") else None
        if cached_version is not None:
            return self._es_major_version
        try:
            version_number = str(self.es.info().get("version", {}).get("number", ""))
            major_version = int(version_number.split(".", 1)[0])
        except Exception:
            major_version = None
        self._es_major_version = major_version
        return major_version

    def _build_vector_script_source(self, query_schema: str) -> str:
        if (self._get_es_major_version() or 0) >= 9:
            return f"cosineSimilarity(params.query_vector, '{query_schema}') + 1.0"
        return f"cosineSimilarity(params.query_vector, doc['{query_schema}']) + 1.0"

    def _build_script_score_query(
        self, query_schema: str, query_vector: list[float], topk: int, filters: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        filter_clauses = list(filters or [])
        filter_clauses.append({"exists": {"field": query_schema}})
        return {
            "size": topk,
            "query": {
                "script_score": {
                    "query": {"bool": {"filter": filter_clauses}},
                    "script": {
                        "source": self._build_vector_script_source(query_schema),
                        "params": {"query_vector": query_vector},
                    },
                }
            },
        }

    def _search_sources(self, table_name: str | None, query: dict) -> list[dict]:
        resp = self.es.search(index=table_name, body=query)
        return [hit["_source"] for hit in self._get_search_hits(resp)]


##################################
class BaseLogConnector(abc.ABC):
    """
    Abstract class for short-term memory storage connectors.
    """

    @abc.abstractmethod
    def read_log(
        self, memory_prefix: str, log_type: str, session_id: str, start_time: Timestamp, end_time: Timestamp, limit: int
    ) -> list[dict]:
        """
        Read logs.

        Args:
            memory_prefix (str): The prefix to use for log entries.
            log_type (str): The type of log to read ('raw', 'trimmed', 'min').
            session_id (str): The session identifier to filter logs (Default: `None`).
            start_time (Timestamp): The start time to filter logs (Default: `None`).
            end_time (Timestamp): The end time to filter logs (Default: `None`).
            limit (int): The maximum number of logs to return (Default: `10`).

        Returns:
            List[dict], a list of log entries as dictionaries.
        """
        pass

    @abc.abstractmethod
    def save_log(
        self,
        memory_prefix: str,
        session_id: str,
        value: dict,
        log_type: str,
        created_at: Timestamp | None,
        updated_at: Timestamp | None,
        ttl_minutes: int | None,
        expires_at: Timestamp | None,
    ) -> None:
        """
        Save logs.

        Args:
            memory_prefix (str): The prefix to use for log entries.
            session_id (str): The session identifier to associate with the log.
            value (dict): The log data to be saved, must be a dictionary.
            log_type (str): The type of log to save ('raw', 'trimmed', 'min').
            created_at (Optional[Timestamp]): The creation timestamp of the log (Default: `None`).
            updated_at (Optional[Timestamp]): The last updated timestamp of the log (Default: `None`).
            ttl_minutes (Optional[int]): The time-to-live in minutes for the log (Default: `None`).
            expires_at (Optional[Timestamp]): The expiration timestamp of the log (Default: `None`).
        """
        pass

    @abc.abstractmethod
    def delete_log(self, memory_prefix: str, session_id: str, log_type: str | None) -> None:
        """
        Delete logs.

        Args:
            memory_prefix (str): The prefix to use for log entries.
            session_id (str): The session identifier to filter logs.
            log_type (str, optional): The type of log to delete ('raw', 'trimmed', 'min'). If `None`, delete from
                all log types (Default: `None`).
        """
        pass


class MySQLReader:
    """
    Connector for MySQL database tables.
    """

    def __init__(self, url: str) -> None:
        """
        Initialize connection to target MySQL database.

        Args:
            url (str): MySQL url to target database.
        """
        self.engine = create_engine(url)

    def load_table(self, table_name: str) -> pd.DataFrame:
        """
        Load table in MySQL to pandas dataframe.

        Args:
            table_name (str): Name of table to be loaded.

        Returns:
            Pd.DataFrame, loaded pandas table.
        """
        sql_command = f"select * from {table_name}"
        df = pd.read_sql(sql_command, con=self.engine)
        return df


class StorageConnectorGaussVector(BaseStorageConnector):
    """
    Storage adaptor to GaussVector.
    """

    def __init__(self, hosts: str = "") -> None:
        """
        Initialize storage adaptor.

        Args:
            hosts (str): Host api of GaussVector (Default: `""`).
        """
        import psycopg2

        match = re.match(r"postgres://(.*):(.*)@(.*):(.*)/(.*)", hosts)
        if match:
            try:
                conn = psycopg2.connect(
                    user=match.group(1),
                    password=match.group(2),
                    host=match.group(3),
                    port=match.group(4),
                    database=match.group(5),
                )
                conn.autocommit = True
                self.gs = conn.cursor()
            except Exception as e:
                raise ValueError("Please input a valid url.") from e
        else:
            raise ValueError("Please input a valid url.")

    def create_table(self, table_name: str, mapping: dict) -> None:
        mapping = mapping["mappings"]["properties"]
        data_type = {}
        for k, v in mapping.items():
            if v["type"] == "keyword":
                data_type[k] = "text"
            elif v["type"] == "dense_vector":
                data_type[k] = "floatvector(1024)"
            elif v["type"] == "integer":
                data_type[k] = "int"

        table_datatype = ""
        for k, v in data_type.items():
            table_datatype += f"{k} {v},"
        table_datatype = table_datatype[:-1]
        self.gs.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({table_datatype})")

    def drop_table(self, table_name: str) -> None:
        self.gs.execute(f"DROP TABLE IF EXISTS {table_name}")

    def insert_data(self, table_name: str, data: dict) -> None:
        columns_list = []
        values_list = []
        for key, value in data.items():
            columns_list.append(key)
            if isinstance(value, str):
                v = value.replace(",", "''")
                values_list.append(f"'{v}'")
            elif isinstance(value, int):
                values_list.append(f"'{value}'")
            elif hasattr(value, "tolist"):
                values_list.append(f"'{str(value.tolist())}'")
            else:
                values_list.append(f"'{str(value)}'")

        columns = ", ".join(columns_list)
        doc_values = ", ".join(values_list)
        self.gs.execute(f"INSERT INTO {table_name} ({columns}) VALUES ({doc_values})")

    def delete_data(
        self, table_name: str, query_schema_list: list[str], query_text_list: list[str | int], query_type: str
    ) -> None:
        query_str = f"DELETE FROM {table_name} WHERE "
        if query_type == "fulltext":
            query_str += f"{query_schema_list[0]} LIKE '%{query_text_list[0]}%'"
            self.gs.execute(query_str)
        elif query_type == "AND":
            for query_schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                query_str += f"{query_schema} = '{query_text}' {query_type} "
            query_str = query_str[:-4]
            self.gs.execute(query_str)
        elif query_type == "OR":
            for query_schema, query_text in zip(query_schema_list, query_text_list, strict=True):
                query_str += f"{query_schema} = '{query_text}' {query_type} "
            query_str = query_str[:-3]
            self.gs.execute(query_str)
        else:
            raise ValueError(f"Unsupported query_type: {query_type}")

    def query_relationship(self, table_name: str, exists_field: str) -> list[dict]:
        query_str = f"SELECT * FROM {table_name} WHERE {exists_field} IS NOT NULL AND {exists_field} != ''"
        return self.execute_sql_and_fetch_dict(query_str)

    def execute_sql_and_fetch_dict(self, sql: str) -> list[dict[str, Any]]:
        self.gs.execute(sql)
        if self.gs.description is not None:
            cur_columns = [desc[0] for desc in self.gs.description]
        else:
            raise ValueError("query sql is error.")

        out = []
        for i in self.gs.fetchall():
            row_dict = {}
            for j, col in enumerate(cur_columns):
                if isinstance(i[j], str) and i[j].startswith("[") and i[j].endswith("]"):
                    row_dict[col] = eval(i[j])
                else:
                    row_dict[col] = i[j]
            out.append(row_dict)
        return out

    def query_fulltext(self, table_name: str, query_schema: str, query_text: str, topk: int) -> list[dict]:
        query_str = f"SELECT * FROM {table_name} WHERE {query_schema} LIKE '%{query_text}%' LIMIT {topk}"
        return self.execute_sql_and_fetch_dict(query_str)

    def query_vector(self, table_name: str, query_schema: str, query_vector: list[float], topk: int) -> list[dict]:
        str_vec = f"[{','.join(map(str, query_vector))}]"
        query_str = f"SELECT * FROM {table_name} ORDER BY {query_schema} <-> '{str_vec}' LIMIT {topk}"
        return self.execute_sql_and_fetch_dict(query_str)

    def query_exact(
        self,
        table_name: str,
        query_schema_list: list[str],
        query_text_list: list[str | int],
        query_type: str,
        topk: int,
    ) -> list[dict]:
        query_str = f"SELECT * FROM {table_name} WHERE "
        for query_schema, query_text in zip(query_schema_list, query_text_list, strict=True):
            query_str += f"{query_schema} = '{query_text}' {query_type} "

        if query_type == "AND":
            query_str = query_str[:-4]
        elif query_type == "OR":
            query_str = query_str[:-3]
        else:
            raise ValueError(f"Unsupported query_type: {query_type}")

        query_str += f"LIMIT {topk}"
        return self.execute_sql_and_fetch_dict(query_str)

    def query_metadata_edge(self, table_name: str, edge_type: str) -> list[dict]:
        if edge_type == "relationship":
            query_str = f"SELECT * FROM {table_name} WHERE relationship != 'has_column' AND relationship != 'None'"
        elif edge_type == "potential_relationship":
            query_str = f"SELECT * FROM {table_name} WHERE potential_relationship != 'None'"
        else:
            raise ValueError("Supported edge types are 'relationship' and 'potential_relationship'.")
        return self.execute_sql_and_fetch_dict(query_str)

    def query_metadata_columns_by_filepath(self, table_name: str, filepath: str) -> list[dict]:
        query_str = f"SELECT * FROM {table_name} WHERE type = 'column' AND path LIKE '%{filepath}%'"
        return self.execute_sql_and_fetch_dict(query_str)

    def query_metadata_column_vector(
        self, table_name: str, query_schema: str, query_vector: list[float], query_type: str, topk: int
    ) -> list[dict]:
        str_vec = f"[{','.join(map(str, query_vector))}]"
        query_str = (
            f"SELECT * FROM {table_name} WHERE type = '{query_type}' "
            f"ORDER BY {query_schema} <-> '{str_vec}' LIMIT {topk}"
        )
        return self.execute_sql_and_fetch_dict(query_str)

    def query_metadata_column_fulltext(
        self, table_name: str, query_schema: str, query_text: str, query_type: str, topk: int
    ) -> list[dict]:
        query_str = (
            f"SELECT * FROM {table_name} WHERE type = '{query_type}' "
            f"AND {query_schema} LIKE '%{query_text}%' LIMIT {topk}"
        )
        return self.execute_sql_and_fetch_dict(query_str)

    def query_all(self, table_name: str) -> list[dict]:
        query_str = f"SELECT * FROM {table_name}"
        return self.execute_sql_and_fetch_dict(query_str)

    def update_value(
        self, table_name: str, pos_query: list[str], pos_text: list[str], update_field: str, update_value: str
    ) -> None:
        query_str = f"UPDATE {table_name} SET {update_field} = '{update_value}' WHERE "
        for query_schema, query_text in zip(pos_query, pos_text, strict=True):
            query_str += f"{query_schema} = '{query_text}' AND "
        query_str = query_str[:-4]
        self.gs.execute(query_str)
