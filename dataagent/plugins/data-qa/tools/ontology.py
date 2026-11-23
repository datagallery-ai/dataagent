"""Database schema tool backed by the host-owned semantic client."""

import asyncio

from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from dataagent import AgentContext


class OntologyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database: str = Field(min_length=1)

    @field_validator("database")
    @classmethod
    def nonblank_database(cls, value):
        if not value.strip():
            raise ValueError("ontology database must not be blank")
        return value


@tool
async def get_ontology_description_tool(runtime: ToolRuntime[AgentContext]) -> dict:
    """Get the configured database's tables, columns and join relationships."""
    database = OntologyConfig.model_validate(runtime.context.get_plugin_config("data-qa")).database
    client = runtime.context.semantic_client
    if client is None:
        raise ValueError("get_ontology_description_tool requires a semantic client")
    tables = await asyncio.to_thread(client.get_table_list, database, limit=1000)
    if tables is None:
        tables = []
    if not isinstance(tables, list):
        raise ToolException("Semantic service table list must be a list")
    tables = [table for table in tables if isinstance(table, dict) and table]
    names = [name for table in tables for name in table]
    columns = {}
    for name in names:
        columns[name] = await asyncio.to_thread(client.get_table_columns_info, name, limit=1000)
    joins = await asyncio.to_thread(client.get_joinable_tables, names) if names else []
    return {"database": database, "tables": tables, "columns": columns, "joins": joins}
