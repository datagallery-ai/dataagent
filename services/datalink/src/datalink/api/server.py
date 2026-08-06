"""DataLink REST API server — HTTP endpoints mirroring CLI commands.

All route names match CLI commands (add-table, rebuild, etc.) for
consistency. Write operations (add-table, rebuild, remove-table, show)
are synchronous — they return the result JSON after completion.

Read operations (explore, search, etc.) are also synchronous but fast.
"""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from datalink.api.deps import get_config, get_retrieval, get_storage, reset_global_instances
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.edge import EdgeType
from datalink.models.node import NodeType
from datalink.utils.credential import mask_result

logger = logging.getLogger(__name__)

app = FastAPI(
    title="DataLink",
    description="REST API for DataLink — all endpoints mirror CLI commands.",
    version="0.1.0",
)

# Suppress INFO-level progress logs in API mode (like MCP)
logging.getLogger("datalink").setLevel(logging.WARNING)


# ── Request models ────────────────────────────────────────────────────


class AddTableRequest(BaseModel):
    source: str = Field(..., description="Data source path or connection string")
    table: str | None = Field(None, description="Table name to add (null = add all tables)")
    source_type: str = Field("csv", description="Source type: csv, parquet, or database")
    schema_name: str | None = Field(None, description="Database schema name")


class RebuildRequest(BaseModel):
    mode: str = Field("full", description="Rebuild mode: full, vec, or profile")


class RemoveTableRequest(BaseModel):
    table_id: str = Field(..., description="Table ID or name to remove")
    cleanup_orphans: bool = Field(True, description="Whether to clean up orphaned Concept/Entity nodes")


class ExploreRequest(BaseModel):
    query: str = Field(..., description="Keywords, names, or natural language description")
    max_nodes: int | None = Field(None, description="Max nodes in detail (null = auto)")
    focus: str | None = Field(None, description="Focus direction: join_paths, schema, data_profile, or null")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search keywords")
    node_type: str | None = Field(None, description="Node type filter: column, table, concept, entity")
    limit: int = Field(10, description="Maximum number of results")


class GetNodeRequest(BaseModel):
    node_id: str = Field(..., description="Node ID (full ID, masked ID, or short alias like table.col)")
    include_edges: bool = Field(True, description="Whether to include adjacent edges")


class PathRequest(BaseModel):
    source_id: str = Field(..., description="Starting node ID (full ID, masked ID, or short alias)")
    target_id: str = Field(..., description="Destination node ID (full ID, masked ID, or short alias)")
    max_depth: int = Field(3, description="Maximum path depth")
    limit: int = Field(3, description="Maximum number of paths to return")
    edge_types: str | None = Field(None, description="Comma-separated edge types to traverse")


class ExtractSubgraphRequest(BaseModel):
    node_ids: str = Field(..., description="Comma-separated node IDs to start from")
    max_hops: int = Field(2, description="Number of neighbor layers to expand")


class PendingEdgesRequest(BaseModel):
    node_id: str | None = Field(None, description="Filter by node ID")
    edge_type: str | None = Field(None, description="Filter by edge type")
    limit: int = Field(50, description="Maximum number of results")


class ConfigUpdateRequest(BaseModel):
    llm_model: str | None = Field(None, description="LLM model name")
    llm_api_key: str | None = Field(None, description="LLM API key")
    llm_base_url: str | None = Field(None, description="LLM API base URL")
    graph_db_path: str | None = Field(None, description="Default graph database path")
    mcp_tools: str | None = Field(None, description="Auxiliary MCP tools to expose (comma-separated)")
    embedding_model: str | None = Field(None, description="Embedding model name")
    embedding_api_key: str | None = Field(None, description="Embedding API key")
    embedding_base_url: str | None = Field(None, description="Embedding API base URL")


# ── Helper: build DatasourceConfig ───────────────────────────────────


def _build_datasource_config(req: AddTableRequest) -> DatasourceConfig:
    """Build a DatasourceConfig from an AddTableRequest."""
    try:
        ds_type = DatasourceType(req.source_type)
    except ValueError:
        raise ValueError(f"Invalid source_type: {req.source_type}")

    resolved_source = req.source
    if ds_type in (DatasourceType.CSV, DatasourceType.PARQUET):
        resolved_source = str(Path(req.source).resolve())

    schema = req.schema_name if req.schema_name else "public"

    return DatasourceConfig(
        type=ds_type,
        path=resolved_source if ds_type in (DatasourceType.CSV, DatasourceType.PARQUET) else "",
        connection_string=req.source if ds_type == DatasourceType.DATABASE else "",
        schema_name=schema,
    )


# ── Write operations (offline) ────────────────────────────────────────


@app.post("/add-table", summary="Add tables from a data source to the graph")
async def add_table(req: AddTableRequest) -> JSONResponse:
    """Add tables from a data source. Mirrors CLI `datalink add-table`."""
    config = get_config()

    try:
        ds_config = _build_datasource_config(req)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        if req.table:
            result = pipeline.add_table(ds_config, req.table)
        else:
            result = pipeline.add_datasource(ds_config)

        reset_global_instances()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        pipeline.close()


@app.post("/rebuild", summary="Rebuild the graph from existing data sources")
async def rebuild(req: RebuildRequest) -> JSONResponse:
    """Rebuild the graph. Mirrors CLI `datalink rebuild`."""
    valid_modes = {"full", "vec", "profile"}
    if req.mode not in valid_modes:
        return JSONResponse(
            {"error": f"Invalid mode: '{req.mode}'. Valid: full, vec, profile"},
            status_code=400,
        )

    config = get_config()

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        result = pipeline.rebuild(mode=req.mode)
        reset_global_instances()

        if result.get("status") == "error":
            return JSONResponse(result, status_code=500)

        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        pipeline.close()


@app.post("/remove-table", summary="Remove a table and all its columns from the graph")
async def remove_table(req: RemoveTableRequest) -> JSONResponse:
    """Remove a table. Mirrors CLI `datalink remove-table`."""
    config = get_config()

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        # Resolve table name → ID if needed
        tid = req.table_id
        if not req.table_id.startswith("table:"):
            retrieval = get_retrieval()
            tables = retrieval.list_datasets(mask_credential=False)
            matching = [t for t in tables if t["name"] == req.table_id]
            if matching:
                tid = matching[0]["id"]
            else:
                return JSONResponse({"error": f"Table '{req.table_id}' not found"}, status_code=404)

        result = pipeline.remove_table(tid, req.cleanup_orphans)
        reset_global_instances()
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        pipeline.close()


@app.get("/show", summary="Show the full graph contents as JSON")
async def show() -> JSONResponse:
    """Show all nodes and edges. Mirrors CLI `datalink show`."""
    config = get_config()
    storage = get_storage(config.graph_db_path)

    try:
        result = storage.show_graph()
        masked = mask_result(result)
        return JSONResponse(masked)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Read operations (online) ──────────────────────────────────────────


@app.post("/explore", summary="Universal retrieval — answers data questions in one call")
async def explore(req: ExploreRequest) -> PlainTextResponse:
    """Explore the data graph. Mirrors CLI `datalink explore`.

    Returns formatted text (same as MCP datalink_explore), not JSON.
    """
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    valid_focuses = {"join_paths", "schema", "data_profile", None}
    if req.focus is not None and req.focus not in valid_focuses:
        return PlainTextResponse(
            f"Invalid focus: '{req.focus}'. Valid: join_paths, schema, data_profile, or omit for balanced.",
            status_code=400,
        )

    result = retrieval.explore(req.query, max_nodes=req.max_nodes, focus=req.focus, mask_credential=True)
    return PlainTextResponse(result)


@app.post("/search", summary="Search nodes by name or semantic type")
async def search(req: SearchRequest) -> JSONResponse:
    """Search for nodes. Mirrors CLI `datalink search`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    nt = None
    if req.node_type:
        try:
            nt = NodeType(req.node_type)
        except ValueError:
            return JSONResponse({"error": f"Invalid node_type: {req.node_type}"}, status_code=400)

    results = retrieval.search_nodes(req.query, nt, req.limit, mask_credential=True)
    return JSONResponse(results)


@app.post("/get-node", summary="Get detailed information about a specific node")
async def get_node(req: GetNodeRequest) -> JSONResponse:
    """Get node details. Mirrors CLI `datalink get-node`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    result = retrieval.get_node(req.node_id, req.include_edges, mask_credential=True)
    if result is None:
        return JSONResponse({"error": f"Node '{req.node_id}' not found"}, status_code=404)
    return JSONResponse(result)


@app.post("/path", summary="Find paths between two nodes")
async def path(req: PathRequest) -> JSONResponse:
    """Find paths. Mirrors CLI `datalink path`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    types = None
    if req.edge_types:
        try:
            type_names = [t.strip() for t in req.edge_types.split(",")]
            types = [EdgeType(t) for t in type_names]
        except ValueError as e:
            return JSONResponse({"error": f"Invalid edge type: {e}"}, status_code=400)

    paths = retrieval.find_paths(
        req.source_id, req.target_id, req.max_depth, types, limit=req.limit, mask_credential=True
    )
    return JSONResponse(paths)


@app.post("/extract-subgraph", summary="Extract a subgraph around specified nodes")
async def extract_subgraph(req: ExtractSubgraphRequest) -> JSONResponse:
    """Extract subgraph. Mirrors CLI `datalink extract-subgraph`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    ids = [id.strip() for id in req.node_ids.split(",")]
    result = retrieval.extract_subgraph(ids, req.max_hops, mask_credential=True)
    return JSONResponse(result)


@app.get("/list-datasets", summary="List all tables/datasets with basic statistics")
async def list_datasets() -> JSONResponse:
    """List datasets. Mirrors CLI `datalink list-datasets`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    datasets = retrieval.list_datasets(mask_credential=True)
    return JSONResponse(datasets)


@app.post("/pending-edges", summary="List pending edges whose referenced nodes are not yet in the graph")
async def pending_edges(req: PendingEdgesRequest) -> JSONResponse:
    """List pending edges. Mirrors CLI `datalink pending-edges`."""
    config = get_config()
    retrieval = get_retrieval(config.graph_db_path)

    et = None
    if req.edge_type:
        try:
            et = EdgeType(req.edge_type)
        except ValueError:
            return JSONResponse({"error": f"Invalid edge type: {req.edge_type}"}, status_code=400)

    nid = req.node_id if req.node_id else None
    results = retrieval.get_pending_edges(nid, et, req.limit, mask_credential=True)
    return JSONResponse(results)


@app.get("/info", summary="Show overview information about the data graph")
async def info() -> JSONResponse:
    """Show graph overview. Mirrors CLI `datalink info`."""
    config = get_config()
    storage = get_storage(config.graph_db_path)

    stats = storage.get_graph_stats()
    return JSONResponse(stats)


# ── Configuration ─────────────────────────────────────────────────────


@app.get("/config", summary="Get current DataLink configuration")
async def get_config_endpoint() -> JSONResponse:
    """Get config. Mirrors CLI `datalink config` (read)."""
    config = get_config()
    # Mask API keys in the response
    safe_config = config.model_dump()
    if safe_config.get("llm", {}).get("api_key"):
        safe_config["llm"]["api_key"] = "***"
    if safe_config.get("embedding", {}).get("api_key"):
        safe_config["embedding"]["api_key"] = "***"
    return JSONResponse(safe_config)


@app.patch("/config", summary="Update DataLink configuration")
async def update_config(req: ConfigUpdateRequest) -> JSONResponse:
    """Update config. Mirrors CLI `datalink config` (write)."""
    config = get_config()

    if req.llm_model:
        config.llm.model = req.llm_model
    if req.llm_api_key:
        config.llm.api_key = req.llm_api_key
    if req.llm_base_url:
        config.llm.base_url = req.llm_base_url
    if req.graph_db_path:
        config.graph_db_path = req.graph_db_path
    if req.mcp_tools:
        config.mcp_tools = req.mcp_tools
    if req.embedding_model:
        config.embedding.model = req.embedding_model
    if req.embedding_api_key:
        config.embedding.api_key = req.embedding_api_key
    if req.embedding_base_url:
        config.embedding.base_url = req.embedding_base_url

    config.save()

    # Return updated config (with masked keys)
    safe_config = config.model_dump()
    if safe_config.get("llm", {}).get("api_key"):
        safe_config["llm"]["api_key"] = "***"
    if safe_config.get("embedding", {}).get("api_key"):
        safe_config["embedding"]["api_key"] = "***"

    return JSONResponse(safe_config)


# ── Server startup ────────────────────────────────────────────────────


def start_api_server(host: str = "0.0.0.0", port: int = 8081) -> None:
    """Start the REST API server using uvicorn."""
    import uvicorn

    logger.info(f"Starting DataLink REST API server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
