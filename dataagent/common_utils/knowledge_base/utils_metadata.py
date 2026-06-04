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
import itertools

import numpy as np
import pandas as pd

from dataagent.common_utils.knowledge_base.utils_common import MySQLReader
from dataagent.common_utils.knowledge_base.utils_inference import cosine_similarity, embedding, model_inference
from dataagent.core.managers.prompt_manager import PROMPT_MD_PREFIX, PromptTemplate


def create_table_schema_template(filename: str, columns: list[str], startnum: int) -> dict:
    """
    Create an empty table metadata template to be filled in.

    Args:
        filename (str): Full path of a file.
        columns (list[str]): List of columns of a file.
        startnum (int): Starting number of id of the file and its columns.

    Returns:
        Dict, an empty table metadata for a file
    """
    out = {filename: {"id": startnum, "file_description": "", "schema": {}}}
    startnum += 1
    for i in columns:
        out[filename]["schema"][i] = {
            "id": startnum,
            "schema_description": "",
            "data_type": "",
            "relationship": [],
            "potential_relationship": [],
        }
        startnum += 1

    return out


def create_tool_schema_template(toolname: str, startnum: int) -> dict:
    """
    Create an empty tool metadata template to be filled in.

    Args:
        toolname (str): Name of a tool.
        startnum (int): Starting number of id of a tool.

    Returns:
        Dict, an empty tool metadata for a file.
    """
    out = {toolname: {"id": startnum, "type": "", "description": "", "parameters": "", "output": "None"}}
    return out


def extract_information(
    index: str,
    metadata: dict,
) -> tuple[list[str], list[str], list[set[str]]]:
    """
    Re-organize the file information in a metadata

    Args:
        index (str): Index of the metadata.
        metadata (dict): Metadata of the files.

    Returns:
        tuple[list[str], list[str], list[set[str]]]: A tuple of column IDs, column descriptions, and column values.
    """

    def load_dataframe(table_path: str) -> pd.DataFrame:
        if table_path.startswith("mysql+pymysql://"):
            url, table = table_path.rpartition("/")[0], table_path.rpartition("/")[-1]
            return MySQLReader(url=url).load_table(table_name=table)

        return pd.read_csv(table_path, keep_default_na=False)

    columns_ids: list[str] = []
    columns_descriptions: list[str] = []
    columns_values: list[set[str]] = []

    for table_path, schemas in metadata.items():
        df = load_dataframe(table_path)
        for col, schema in schemas["schema"].items():
            columns_ids.append(f"{table_path} -> {col}")
            columns_descriptions.append(schema["schema_description"])
            try:
                values = df[col].unique()
            except Exception:
                values = df[col].astype(str).unique()

            columns_values.append(set(values))

    return columns_ids, columns_descriptions, columns_values


def filter_by_similarity_and_distance(
    col_ids1: list[str],
    col_ids2: list[str],
    similarity_matrix: np.ndarray,
    columns_values1: list[set[str]],
    columns_values2: list[set[str]],
    similarity_threshold: float,
    distance_threshold: float,
) -> list[tuple[str, str]]:
    """
    Filter out columns with low similarity in column description or with large distance in set of column values.

    Args:
        col_ids1 (list[str]): One collection of column names in the format of '{file name} -> {column name}'
        col_ids2 (list[str]): The other collection of column names in the format of '{file name} -> {column name}'
        similarity_matrix (np.ndarray): 2d matrix of cosine similarity between two collections of column descriptions
            with shape (len(col_ids1), len(col_ids2)).
        columns_values1 (list[set[str]]): One collection of unique column values.
        columns_values2 (list[set[str]]): The other collection of unique of column values.
        similarity_threshold (float): Cosine similarity threshold between column descriptions.
        distance_threshold (float): Pre-defined distance threshold between unique column values.

    Returns:
        list[tuple[str, str]], list of pairs of columns, in the format of '{file name} -> {column name}',
        with high column description similarity and low distance between unique column values.
    """
    results: list[tuple[str, str]] = []

    for i, j in itertools.product(range(len(col_ids1)), range(len(col_ids2))):
        set1 = columns_values1[i]
        set2 = columns_values2[j]
        distance = 1.0 if not set1 or not set2 else min(len(set1 - set2) / len(set1), len(set2 - set1) / len(set2))
        if similarity_matrix[i, j] >= similarity_threshold and distance <= distance_threshold:
            results.append((col_ids1[i], col_ids2[j]))

    return results


def infer_file_description(filename: str, document: str, columns: list[str], file_description: str) -> str:
    """
    Extract or refine file descriptions from a piece of knowledge.

    Args:
        filename (str): Name of a file.
        document (str): Piece of knowledge information.
        columns (list[str]): List of column names.
        file_description (str): Current file description.

    Returns:
        str, refined file description.
    """
    state = {
        "filename": filename,
        "document": document.strip().strip(" "),
        "columns": ", ".join(columns),
        "file_description": file_description,
    }
    message = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/knowledge_base/infer_file_description"
    ).apply_prompt_template(**state)
    return model_inference(message)


def infer_schema_description(knowledge: str, columns_info: dict) -> str:
    """
    Infer the schema description based on knowledge and column information.

    Args:
        knowledge (str): Knowledge about Files.
        columns_info (dict): Information about columns

    Returns:
        Str, description of schema.
    """
    cur_columns_info = []
    for col_name, col_info in columns_info.items():
        col_str = f"Column Name: {col_name}\nCurrent Description: {col_info['description']}\nSample Values: \
            {', '.join(col_info['sampled_values'])}"
        cur_columns_info.append(col_str)

    # new Infer columns description
    columns_info_str = "\n\n".join(cur_columns_info)
    state = {"knowledge": knowledge, "columns_info_str": columns_info_str}
    message = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/knowledge_base/infer_schema_description"
    ).apply_prompt_template(**state)
    return model_inference(message)


def infer_data_type(columns_info: dict, outputs: list[str] | None = None) -> str:
    """
    Infer the data type based on column information and outputs type.

    Args:
        columns_info (dict): Information about columns
        outputs (list[str] | None): Range of required output types
            (Default: ["Categorical", "Date", "Numeric", "Boolean", "ID", "Path", "Text"]).

    Returns:
        Str, the inferred type of each column.
    """
    if not outputs:
        outputs = ["Categorical", "Date", "Numeric", "Boolean", "ID", "Path", "Text"]
    cur_column_info = "\n".join(
        [
            f"Column: {name}\nDescription: {info.get('description', '')}\nSample Values: \
            {', '.join(map(str, info.get('sampled_values', [])[:10]))}\n"
            for name, info in columns_info.items()
        ]
    )
    state = {"outputs": outputs, "cur_column_info": cur_column_info}
    message = PromptTemplate.from_package_relative(
        f"{PROMPT_MD_PREFIX}/knowledge_base/infer_data_type"
    ).apply_prompt_template(**state)
    return model_inference(message)


def infer_joinable_relationship(
    col_ids1: list[str],
    col_ids2: list[str],
    columns_descriptions1: list[str],
    columns_descriptions2: list[str],
    columns_values1: list[set[str]],
    columns_values2: list[set[str]],
    similarity_threshold: float = 0.4,
    distance_threshold: float = 0.0,
    *,
    embedding_model: str,
) -> list[tuple[str, str]]:
    """
    Infer joinable relationship between columns.

    Args:
        col_ids1 (list[str]): One collection of column names in the format of '{file name} -> {column name}'.
        col_ids2 (list[str]): The other collection of column names in the format of '{file name} -> {column name}'.
        columns_descriptions1 (list[str]): One collection of column descriptions.
        columns_descriptions2 (list[str]): The other collection of column descriptions.
        columns_values1 (list[set[str]]): One collection of unique column values.
        columns_values2 (list[set[str]]): The other collection of unique column values.
        similarity_threshold (float, optional): Cosine similarity threshold between column descriptions
            (Default: `0.4`).
        distance_threshold (float, optional): Pre-defined distance threshold between unique column values
            (Default: `0.0`).
        embedding_model: Model key from per-Agent ``MEMORY.embedding_model`` (required).

    Returns:
        List[tuple[str, str]], list of pairs of columns to be marked as 'joinable', in the format of
            '{file name} -> {column name}'
    """
    embs1 = embedding(columns_descriptions1, embedding_model=embedding_model)
    embs2 = embedding(columns_descriptions2, embedding_model=embedding_model)
    similarity = cosine_similarity(embs1, embs2)
    out = filter_by_similarity_and_distance(
        col_ids1=col_ids1,
        col_ids2=col_ids2,
        similarity_matrix=similarity,
        columns_values1=columns_values1,
        columns_values2=columns_values2,
        similarity_threshold=similarity_threshold,
        distance_threshold=distance_threshold,
    )
    return out
