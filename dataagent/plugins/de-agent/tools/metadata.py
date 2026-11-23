"""Native tools using the host semantic client and plugin configuration."""

import re

from langchain.tools import ToolRuntime
from utils.evidence import save_evidence
from utils.semantic_tools import semantic_tool

from dataagent import AgentContext


@semantic_tool
def get_table_schema(table_name: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Retrieve all columns of an evidenced db.table, preserving its database prefix."""
    return _get_table_schema(table_name, runtime)


@semantic_tool
def get_join_relations(table_names: list[str], runtime: ToolRuntime[AgentContext]) -> dict:
    """Retrieve known join relationships for fully qualified table names."""
    for name in table_names:
        _table_name(name)
    data = _client(runtime).get_joinable_tables(table_names)
    return save_evidence(runtime.context, "joins", table_names, data)


@semantic_tool
def search_udf_function_by_name_keyword(keyword: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Search UDF names and descriptions and retain the returned prototypes and examples."""
    criterion = []
    for attribute in ("name", "function_description", "prototype", "category"):
        criterion.append(
            {"attributeName": attribute, "operator": "CONTAINS_LIKE", "attributeValue": keyword}
        )
    data = _client(runtime).search_basic(
        {
            "typeName": "udf_function",
            "offset": 0,
            "limit": 25,
            "entityFilters": {"condition": "OR", "criterion": criterion},
            "attributes": [
                "qualified_name",
                "prototype",
                "args",
                "function_description",
                "examples",
            ],
        }
    )
    return save_evidence(runtime.context, "udf", keyword, data)


@semantic_tool
def search_udf_function_by_dsl(
    attribute_name: str,
    attribute_value: str,
    runtime: ToolRuntime[AgentContext],
) -> dict:
    """Search UDF metadata by one known attribute and fetch each matching entity's details."""
    if attribute_name not in {
        "name",
        "function_description",
        "prototype",
        "category",
        "description",
        "type",
    }:
        raise ValueError("Unsupported UDF search attribute")
    value = attribute_value.replace("\\", "\\\\").replace('"', '\\"')
    client = _client(runtime)
    raw = client.search_dsl(f'udf_function where {attribute_name} like "*{value}*"')
    entities = []
    for entity in raw.get("entities", []):
        guid = entity.get("guid")
        entities.append(client.get_entity_by_guid(guid) if guid else entity)
    return save_evidence(
        runtime.context, "udf", {attribute_name: attribute_value}, {"entities": entities}
    )


def validate_table_name(table_name: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Validate a user-specified db.table against plugin-configured allowed databases."""
    try:
        _table_name(table_name)
        allowed = runtime.context.get_plugin_config("de-agent").get("allowed_databases", [])
        if allowed and table_name.split(".", 1)[0] not in allowed:
            raise ValueError(f"Database is not in allowed_databases: {allowed}")
    except ValueError as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True, "error": None}


@semantic_tool
def check_table_existence(table_name: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Check metadata existence; service errors propagate instead of meaning 'table absent'."""
    result = _get_table_schema(table_name, runtime)
    return {**result, "exists": bool(result.get("data"))}


@semantic_tool
def query_sql_process(target_table: str, runtime: ToolRuntime[AgentContext]) -> dict:
    """Retrieve an existing table's INSERT expression for an incremental add-columns task."""
    _table_name(target_table)
    result = _client(runtime).get_entity_by_unique_attribute(
        "sql_process", "target_table", target_table
    )
    expression = result.get("entity", {}).get("attributes", {}).get("expression", "")
    saved = save_evidence(runtime.context, "sql_process", target_table, result)
    return {**saved, "expression": expression, "target_table": target_table}


def _client(runtime):
    client = runtime.context.semantic_client
    if client is None:
        raise ValueError("de-agent requires the Agent's semantic configuration")
    return client


def _table_name(value):
    if re.fullmatch(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*", value) is None:
        raise ValueError("Table name must use lowercase db.table identifiers")


def _get_table_schema(table_name, runtime):
    _table_name(table_name)
    data = _client(runtime).get_table_columns_info(table_name, limit=1000)
    return save_evidence(runtime.context, "schema", table_name, data)
