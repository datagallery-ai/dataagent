"""DataLink MCP Server — expose retrieval interfaces as MCP tools.

Default surface: datalink_explore (universal retrieval) only.

Write operations (add_table, rebuild, remove_table) and show are
available via the REST API (datalink api) — they are offline/management
operations that don't belong in an agent's retrieval context.

Auxiliary tools (search, get_node, etc.) are available via DATALINK_MCP_TOOLS
environment variable but not registered by default.
"""

import logging
import os

from mcp.server.fastmcp import FastMCP

# MCP server: suppress INFO-level progress logs (CLI shows them, MCP doesn't need them)
logging.getLogger("datalink").setLevel(logging.WARNING)

from datalink.config import DataLinkConfig  # noqa: E402
from datalink.graph.retrieval import GraphRetrieval  # noqa: E402
from datalink.graph.storage import GraphStorage  # noqa: E402
from datalink.models.node import NodeType  # noqa: E402

logger = logging.getLogger(__name__)

# Create the MCP server
# Default host="0.0.0.0" so the server listens on all interfaces, not just localhost.
# This allows remote clients (other machines) to connect — essential when the MCP
# server runs on a shared / cloud host.  Local-only use still works fine on 0.0.0.0.
_DEFAULT_HOST = os.environ.get("DATALINK_MCP_HOST", "0.0.0.0")
mcp = FastMCP("DataLink", host=_DEFAULT_HOST)

# Global storage and retrieval instances (initialized on startup)
_storage: GraphStorage | None = None
_retrieval: GraphRetrieval | None = None


def _get_storage(db_path: str) -> GraphStorage:
    """Get or create the storage instance."""
    global _storage
    if _storage is None:
        _storage = GraphStorage(db_path)
    return _storage


def _get_retrieval(db_path: str) -> GraphRetrieval:
    """Get or create the retrieval instance."""
    global _retrieval
    if _retrieval is None:
        storage = _get_storage(db_path)
        _retrieval = GraphRetrieval(storage)
    return _retrieval


def _get_tool_allowlist() -> set[str] | None:
    """Get auxiliary MCP tool allowlist — env var takes precedence over config.

    Sources (in priority order):
    1. DATALINK_MCP_TOOLS env var — comma-separated full tool names
    2. mcp_tools field in datalink_config.json

    Returns None if both are unset/empty (meaning: only default tools exposed).
    Returns a set of tool names if either source provides one, e.g.
      {"datalink_search_nodes", "datalink_get_node"}
    """
    # Env var (highest priority)
    env_raw = os.environ.get("DATALINK_MCP_TOOLS", "")
    if env_raw.strip():
        return set(t.strip() for t in env_raw.split(",") if t.strip())

    # Config file (fallback)
    config = DataLinkConfig.load()
    if config.mcp_tools.strip():
        return set(t.strip() for t in config.mcp_tools.split(",") if t.strip())

    return None


# ── Core tool: always registered ────────────────────────────────────


@mcp.tool()
def datalink_explore(
    query: str,
    max_nodes: int | None = None,
    focus: str | None = None,
    mask_credential: bool = True,
) -> str:
    """PRIMARY TOOL — call FIRST for data questions: how is X related to Y,
    what columns can I JOIN, what does 'revenue' mean in this data,
    which tables share similar fields. Returns organized context about
    relevant nodes (column details, profiles, relationships) in ONE call,
    plus the relationship map between them. ONE call to explore is usually
    sufficient — no need to follow up with search + get_node + find_paths
    to get the same information piece by piece.

    Args:
        query: Keywords, names, or short descriptions (e.g., "revenue customer_id orders",
               "how is orders connected to customers", "email address fields").
               Use space-separated keywords, not SQL expressions.
        max_nodes: Max nodes in detail (default: auto-scaled by project size).
        focus: Optional focus direction — 'join_paths' (relationship chains),
               'schema' (table structure), 'data_profile' (column fingerprints and quality),
               or None for balanced output.
        mask_credential: Whether to mask database credentials in output (default True).
               When True, usernames and passwords in connection strings are replaced with ***.
               Set to False only if the agent needs the actual connection string to access the database.
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    # Validate focus
    valid_focuses = {"join_paths", "schema", "data_profile", None}
    if focus is not None and focus not in valid_focuses:
        return f"Invalid focus: '{focus}'. Valid: join_paths, schema, data_profile, or omit for balanced."

    return retrieval.explore(query, max_nodes=max_nodes, focus=focus, mask_credential=mask_credential)


# ── Auxiliary tools: only registered if DATALINK_MCP_TOOLS enables them ──
# These are plain functions — they get registered as MCP tools via mcp.tool()
# only when start_mcp_server() decides to include them based on the allowlist.


def _datalink_search_nodes(
    query: str,
    node_type: str | None = None,
    limit: int = 10,
    mask_credential: bool = True,
) -> str:
    """Search for nodes in the data graph by name or semantic type.
    Use space-separated keywords, not SQL expressions.
    Returns a list of matching nodes with brief relationship summaries.

    Args:
        query: Search string — keywords or short descriptions (e.g., "customer email address",
               "revenue amount", "free meal count"). Not a SQL WHERE clause.
        node_type: Optional filter: 'column', 'table', 'concept', 'entity'.
        limit: Maximum number of results (default 10).
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    nt = None
    if node_type:
        try:
            nt = NodeType(node_type)
        except ValueError:
            return f"Invalid node_type: {node_type}. Valid types: column, table, concept, entity."

    results = retrieval.search_nodes(query, nt, limit, mask_credential=mask_credential)
    return retrieval.format_search_nodes_text(results, query, mask_credential=mask_credential)


def _datalink_get_node(
    node_id: str,
    include_edges: bool = True,
    mask_credential: bool = True,
) -> str:
    """Get detailed information about a specific node, including its adjacent edges.

    Args:
        node_id: Node ID to retrieve. Valid formats:
          • Full ID: type:source:table[:column] — e.g. column:sqlite:///.../db.sqlite:frpm:County Name
          • Masked ID: same format with credentials masked (***:***@host) — auto-resolved internally
          • Short alias: table_name.column_name or bare name — e.g. "frpm.County Name", "frpm", "County Name"
          Supported type prefixes: table, column, concept, entity.
          "profile:column:..." is NOT a valid node_id — profiles are attached to column nodes, not standalone.
        include_edges: Whether to include adjacent edge information (default true).
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    result = retrieval.get_node(node_id, include_edges, mask_credential=mask_credential)
    if result is None:
        return (
            f'Node "{node_id}" not found. This may be due to:\n'
            f"  1. Wrong ID format — valid formats: column:source:table:col_name, "
            f"or short alias like table_name.col_name\n"
            f"  2. Column name mismatch — the actual column name may differ "
            f"(spaces, hyphens, special chars)\n"
            f"  3. Node does not exist in this graph\n"
            f"Use datalink_search_nodes or datalink_explore to discover correct node IDs."
        )
    return retrieval.format_get_node_text(result, mask_credential=mask_credential)


def _datalink_find_paths(
    source_id: str,
    target_id: str,
    max_depth: int = 3,
    limit: int = 3,
    edge_types: str | None = None,
    mask_credential: bool = True,
) -> str:
    """Find paths between two nodes in the data graph.

    Args:
        source_id: Starting node ID. Valid formats: full ID (type:source:table[:column]),
                   masked ID (auto-resolved), or short alias (table_name.col_name).
        target_id: Destination node ID. Same format rules as source_id.
        max_depth: Maximum path length (default 3).
        limit: Maximum number of paths to return (default 3).
        edge_types: Optional comma-separated list of edge types to traverse
                    (e.g., 'joinable,semantic_synonym,foreign_key').
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    from datalink.models.edge import EdgeType

    types = None
    if edge_types:
        try:
            type_names = [t.strip() for t in edge_types.split(",")]
            types = [EdgeType(t) for t in type_names]
        except ValueError as e:
            valid = "foreign_key, joinable, semantic_synonym, semantic_type_match, "
            valid += "distribution_similar, correlated, represents, has_concept, contains"
            return f"Invalid edge type: {e}. Valid types: {valid}."

    paths = retrieval.find_paths(source_id, target_id, max_depth, types, limit=limit, mask_credential=mask_credential)
    # Resolve source/target IDs for display (they may have been alias-resolved internally)
    resolved_source = retrieval._resolve_input_id(source_id)
    resolved_target = retrieval._resolve_input_id(target_id)
    return retrieval.format_find_paths_text(paths, resolved_source, resolved_target, mask_credential=mask_credential)


def _datalink_extract_subgraph(
    node_ids: str,
    max_hops: int = 2,
    mask_credential: bool = True,
) -> str:
    """Extract a subgraph around specified nodes, expanding by neighbor hops.

    Args:
        node_ids: Comma-separated list of node IDs to start from. Valid formats:
                  full ID (type:source:table[:column]), masked ID (auto-resolved),
                  or short alias (table_name.col_name).
        max_hops: Number of neighbor layers to expand (default 2).
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    ids = [id.strip() for id in node_ids.split(",")]
    result = retrieval.extract_subgraph(ids, max_hops, mask_credential=mask_credential)
    return retrieval.format_extract_subgraph_text(result, mask_credential=mask_credential)


def _datalink_list_datasets(mask_credential: bool = True) -> str:
    """List all tables/datasets in the data graph with basic statistics.

    Args:
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    datasets = retrieval.list_datasets(mask_credential=mask_credential)
    return retrieval.format_list_datasets_text(datasets, mask_credential=mask_credential)


def _datalink_list_pending_edges(
    node_id: str = "",
    edge_type: str = "",
    limit: int = 50,
    mask_credential: bool = True,
) -> str:
    """List pending edges whose referenced nodes are not yet in the graph.

    Args:
        node_id: Optional — filter to pending edges involving this node ID.
                  Valid formats: full ID, masked ID, or short alias (table_name.col_name).
        edge_type: Optional — filter by edge type (e.g., 'foreign_key', 'joinable').
        limit: Maximum number of results (default 50).
        mask_credential: Whether to mask database credentials in output (default True).
    """
    config = DataLinkConfig.load()
    retrieval = _get_retrieval(config.graph_db_path)

    from datalink.models.edge import EdgeType

    et = None
    if edge_type:
        try:
            et = EdgeType(edge_type)
        except ValueError:
            valid = "foreign_key, joinable, semantic_synonym, semantic_type_match, "
            valid += "distribution_similar, correlated, represents, has_concept, contains"
            return f"Invalid edge type: {edge_type}. Valid types: {valid}."

    nid = node_id if node_id else None
    results = retrieval.get_pending_edges(nid, et, limit, mask_credential=mask_credential)
    return retrieval.format_list_pending_edges_text(results, mask_credential=mask_credential)


# ── Auxiliary tool registry ──────────────────────────────────────────

# Map of auxiliary tool name → (function, description for MCP registration)
AUXILIARY_TOOLS: dict[str, tuple] = {
    "datalink_search_nodes": (_datalink_search_nodes, "Search nodes by name or semantic type."),
    "datalink_get_node": (_datalink_get_node, "Detailed node info with adjacent edges."),
    "datalink_find_paths": (_datalink_find_paths, "Find paths between two nodes."),
    "datalink_extract_subgraph": (_datalink_extract_subgraph, "Extract subgraph around nodes."),
    "datalink_list_datasets": (_datalink_list_datasets, "List datasets with statistics."),
    "datalink_list_pending_edges": (_datalink_list_pending_edges, "List pending edges with missing endpoints."),
}


def _register_auxiliary_tools(allowlist: set[str]) -> None:
    """Register auxiliary tools that appear in the allowlist."""
    for tool_name, (func, desc) in AUXILIARY_TOOLS.items():
        if tool_name in allowlist:
            mcp.tool(name=tool_name, description=desc)(func)
            logger.info(f"Registered auxiliary MCP tool: {tool_name}")


# ── Server startup ───────────────────────────────────────────────────


def start_mcp_server(
    db_path: str = "datalink.db",
    port: int = 8080,
    transport: str = "sse",
    host: str = "",
) -> None:
    """Start the MCP server.

    Always registers: datalink_explore (universal retrieval).
    Optionally registers auxiliary tools based on DATALINK_MCP_TOOLS env var.

    Write operations (add_table, rebuild, remove_table) and show are
    available via the REST API (datalink api command) instead.

    Args:
        db_path: Path to the graph database.
        port: Port to listen on.
        transport: Transport protocol — 'sse' (legacy) or 'streamable-http' (recommended).
        host: Bind address. Empty string = use the default (0.0.0.0, or
              DATALINK_MCP_HOST env var). Pass '127.0.0.1' for localhost-only.
    """
    # Initialize storage
    config = DataLinkConfig.load()
    config.graph_db_path = db_path
    config.save()

    _get_storage(db_path)

    # Register auxiliary tools if allowlist is set
    allowlist = _get_tool_allowlist()
    if allowlist:
        _register_auxiliary_tools(allowlist)

    effective_host = host or mcp.settings.host
    logger.info(f"Starting DataLink MCP server on {effective_host}:{port}")
    if allowlist:
        logger.info(f"Auxiliary tools enabled: {sorted(allowlist)}")
    else:
        logger.info(
            "Only core tool exposed (datalink_explore). "
            "Set DATALINK_MCP_TOOLS to enable auxiliary tools. "
            "Write operations (add-table, rebuild, remove-table) and show are available via REST API."
        )

    if host:
        mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport=transport)
