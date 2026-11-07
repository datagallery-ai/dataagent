"""DataLink CLI — command-line interface for building and querying the data graph."""

import logging
import os
import re
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from datalink.config import DataLinkConfig
from datalink.models.datasource import DatasourceConfig, DatasourceType
from datalink.models.node import NodeType

app = typer.Typer(
    name="datalink",
    help="DataLink — Build and query a data-focused knowledge graph.",
    add_completion=False,
)

# Force UTF-8 output on Windows to avoid UnicodeEncodeError with characters
# like → ← ↔ ⚠️ that the default GBK codec cannot encode.
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    # Reconfigure stdout/stderr to UTF-8 if they are still using the default encoding
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

_console_base = Console()
# Use a wider fallback width when output is piped (Rich defaults to 80
# which over-compresses tables).  Real terminals use their actual width.
console = Console(width=160 if not _console_base.is_terminal else None)

# Configure logging — INFO level to stderr for CLI progress visibility.
# MCP server overrides this to WARNING at startup (see mcp/server.py).
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)


def _detect_datasource_type(source: str) -> DatasourceType:
    """Auto-detect datasource type from the source string."""
    # Match both bare scheme (mysql://) and dialect+driver (mysql+pymysql://)
    if re.match(r"^(?:postgresql|mysql|sqlite|mssql|oracle|clickhouse|mariadb)(?:\+\w+)?://", source, re.IGNORECASE):
        return DatasourceType.DATABASE
    elif source.endswith(".csv") or source.endswith("/") or source.endswith("\\"):
        return DatasourceType.CSV
    elif source.endswith(".parquet") or source.endswith(".pq"):
        return DatasourceType.PARQUET
    else:
        # Check if it's a directory with CSV/Parquet files
        from pathlib import Path

        path = Path(source)
        if path.is_dir():
            if list(path.glob("*.csv")):
                return DatasourceType.CSV
            if list(path.glob("*.parquet")):
                return DatasourceType.PARQUET
        return DatasourceType.CSV  # Default fallback


def _make_datasource_config(source: str) -> DatasourceConfig:
    """Build a DatasourceConfig from a source string.

    For file-type sources (CSV/Parquet), relative paths are resolved to
    absolute paths so that node IDs and TableNode.source are unique even
    when the same filename exists in different directories.
    """
    ds_type = _detect_datasource_type(source)

    # Resolve file paths to absolute
    resolved_source = source
    if ds_type in (DatasourceType.CSV, DatasourceType.PARQUET):
        from pathlib import Path

        resolved_source = str(Path(source).resolve())

    return DatasourceConfig(
        type=ds_type,
        path=resolved_source if ds_type in (DatasourceType.CSV, DatasourceType.PARQUET) else "",
        connection_string=source if ds_type == DatasourceType.DATABASE else "",
    )


# ── Write commands ─────────────────────────────────────────────────────


@app.command()
def add_table(
    source: str = typer.Option(..., "--source", "-s", help="Data source path or connection string"),
    table: str = typer.Option("", "--table", "-t", help="Table name to add (empty = add all tables from source)"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Add tables from a data source to the graph.

    If --table is specified, adds only that table.
    If --table is omitted, adds all tables from the data source.
    On an empty graph this serves as the initial build.
    """
    config = DataLinkConfig.load()
    config.graph_db_path = db_path

    ds_config = _make_datasource_config(source)

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        if table:
            console.print(f"[bold blue]Adding table '{table}'...[/bold blue]")
            result = pipeline.add_table(ds_config, table)
            if result["status"] == "skipped":
                console.print(f"[bold yellow]⚠ Table '{table}' already exists — skipped[/bold yellow]")
                console.print(f"  Use 'datalink remove-table --table {table}' first to re-add it.")
            else:
                console.print(f"[bold green]✓ Added table '{table}'[/bold green]")
        else:
            console.print("[bold blue]Adding all tables from source...[/bold blue]")
            result = pipeline.add_datasource(ds_config)
            added = result.get("added_tables", [])
            skipped = result.get("skipped_tables", [])
            if added:
                console.print(f"[bold green]✓ Added {len(added)} tables: {added}[/bold green]")
            if skipped:
                console.print(f"[bold yellow]⚠ Skipped {len(skipped)} existing tables: {skipped}[/bold yellow]")
                console.print("  Use 'datalink remove-table' first to re-add them.")
            if not added and skipped:
                console.print("[bold yellow]All tables already exist — nothing to add.[/bold yellow]")

        stats = result["stats"]
        console.print(f"  Tables: {stats['node_type_counts']['table']}")
        console.print(f"  Columns: {stats['node_type_counts']['column']}")
        console.print(f"  Total Edges: {stats['total_edges']}")
    except Exception as e:
        console.print(f"[bold red]Add table failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        pipeline.close()


@app.command()
def rebuild(
    mode: str = typer.Option(
        "full",
        "--mode",
        "-m",
        help="Rebuild mode: full, vec, profile",
    ),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Rebuild the graph from existing data sources.

    Three rebuild modes:

    - full: Clear the graph and re-run the entire pipeline (original behavior),
      now also includes embedding vector construction.

    - vec: Re-build embedding vectors only using the current embedding model.
      No data changes, no LLM calls. Use this when you change the embedding
      model configuration and need to update all vectors.

    - profile: Re-compute column/table statistics and profile-dependent inferred
      edges (joinable, distribution_similar, synonym, correlated). No semantic
      layer changes, no LLM calls. Use this when data has changed but the
      semantic structure should remain stable.

    **Safety guarantee (mode=full)**: old data is only cleared AFTER the new
    pipeline succeeds. If the pipeline fails, the old graph data is preserved.
    """
    valid_modes = {"full", "vec", "profile"}
    if mode not in valid_modes:
        console.print(f"[bold red]Invalid mode: '{mode}'. Valid: full, vec, profile[/bold red]")
        raise typer.Exit(code=1)

    console.print(f"[bold blue]Rebuilding DataLink (mode={mode})...[/bold blue]")

    config = DataLinkConfig.load()
    config.graph_db_path = db_path

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        result = pipeline.rebuild(mode=mode)

        if result["status"] == "error":
            console.print(f"[bold red]Rebuild failed:[/bold red] {result['error']}")
            raise typer.Exit(code=1)

        stats = result["stats"]

        if mode == "vec":
            table = Table(title="Vec Rebuild Results")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Vectors Built", str(result.get("vectors_built", 0)))
            table.add_row("Model", result.get("model", ""))
            table.add_row("Total Nodes", str(stats["total_nodes"]))
            console.print(table)
            console.print("[bold green]Vector rebuild complete![/bold green]")

        elif mode == "profile":
            table = Table(title="Profile Rebuild Results")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")
            table.add_row("Profiles Updated", str(result.get("profiles_updated", 0)))
            table.add_row("Edges Removed (old)", str(result.get("edges_removed", 0)))
            table.add_row("Edges Rebuilt (new)", str(result.get("edges_rebuilt", 0)))
            table.add_row("Tables", str(stats["node_type_counts"]["table"]))
            console.print(table)
            console.print("[bold green]Profile rebuild complete![/bold green]")

        else:  # mode == "full"
            table = Table(title="Full Rebuild Results")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="green")

            table.add_row("Datasources", str(result["datasources"]))
            table.add_row("Tables", str(result["tables"]))
            table.add_row("Columns", str(stats["node_type_counts"]["column"]))
            table.add_row("Concepts", str(stats["node_type_counts"]["concept"]))
            table.add_row("Entities", str(stats["node_type_counts"]["entity"]))
            table.add_row("Total Edges", str(stats["total_edges"]))
            table.add_row("Contains Edges", str(stats["edge_type_counts"]["contains"]))
            table.add_row("FK Edges", str(stats["edge_type_counts"]["foreign_key"]))
            table.add_row("Joinable Edges", str(stats["edge_type_counts"]["joinable"]))
            table.add_row("Synonym Edges", str(stats["edge_type_counts"]["semantic_synonym"]))
            table.add_row("Distribution Edges", str(stats["edge_type_counts"]["distribution_similar"]))
            table.add_row("Correlated Edges", str(stats["edge_type_counts"]["correlated"]))
            console.print(table)
            console.print("[bold green]Full rebuild complete![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Rebuild failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        pipeline.close()


@app.command()
def show(
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Show the full graph contents (nodes and edges) as JSON."""
    import json as json_mod

    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)

    try:
        result = storage.show_graph()
        console.print(json_mod.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        console.print(f"[bold red]Show failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        storage.close()


@app.command()
def remove_table(
    table: str = typer.Option(..., "--table", "-t", help="Table ID or name to remove"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Remove a table and all its columns from the graph."""
    console.print(f"[bold blue]Removing table '{table}'...[/bold blue]")

    config = DataLinkConfig.load()
    config.graph_db_path = db_path

    from datalink.builder.pipeline import BuildPipeline

    pipeline = BuildPipeline(config)

    try:
        # If table is a name, find its ID
        table_id = table
        if not table.startswith("table:"):
            # Search for the table by name
            from datalink.graph.retrieval import GraphRetrieval

            retrieval = GraphRetrieval(pipeline.storage)
            tables = retrieval.list_datasets(mask_credential=False)
            matching = [t for t in tables if t["name"] == table]
            if matching:
                table_id = matching[0]["id"]
            else:
                console.print(f"[bold red]Table '{table}' not found[/bold red]")
                raise typer.Exit(code=1)

        result = pipeline.remove_table(table_id)
        console.print("[bold green]Removed table[/bold green]")
        console.print(f"  Removed columns: {result['removed_columns']}")
        console.print(f"  Removed orphaned concepts/entities: {result['removed_orphans']}")
    except Exception as e:
        console.print(f"[bold red]Remove table failed:[/bold red] {e}")
        raise typer.Exit(code=1)
    finally:
        pipeline.close()


# ── Read commands ──────────────────────────────────────────────────────


@app.command()
def explore(
    query: str = typer.Argument(..., help="Search query (keywords, names, natural language)"),
    focus: str = typer.Option("", "--focus", "-f", help="Focus: join_paths, schema, data_profile"),
    max_nodes: int = typer.Option(0, "--max-nodes", "-n", help="Max nodes (0=auto)"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Explore the data graph — one call answers the whole question."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    focus_val = focus if focus else None
    max_nodes_val = max_nodes if max_nodes > 0 else None

    if focus_val and focus_val not in ("join_paths", "schema", "data_profile"):
        console.print(f"[bold red]Invalid focus: {focus}. Valid: join_paths, schema, data_profile[/bold red]")
        raise typer.Exit(code=1)

    result = retrieval.explore(query, max_nodes=max_nodes_val, focus=focus_val, mask_credential=False)
    console.print(Panel(result, title=f"Explore: {query}", border_style="blue"))
    storage.close()


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    type: str = typer.Option("", "--type", "-t", help="Node type filter (column, table, concept, entity)"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum number of results"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Search for nodes in the data graph."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    node_type = None
    if type:
        try:
            node_type = NodeType(type)
        except ValueError:
            console.print(f"[bold red]Invalid node type: {type}[/bold red]")
            raise typer.Exit(code=1)

    results = retrieval.search_nodes(query, node_type, limit, mask_credential=False)

    if not results:
        console.print(f"[yellow]No results found for '{query}'[/yellow]")
        return

    table = Table(title=f"Search Results for '{query}'", show_lines=True)
    table.add_column("Name", style="green")
    table.add_column("Type", style="blue")
    table.add_column("Edges", style="yellow")
    table.add_column("ID", style="cyan", max_width=60, overflow="ellipsis", no_wrap=True)

    for r in results:
        table.add_row(r["name"], r["type"], str(r["edge_count"]), r["id"])

    console.print(table)
    storage.close()


@app.command()
def info(
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Show overview information about the data graph."""
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)

    stats = storage.get_graph_stats()

    panel = Panel(
        f"[bold]DataLink Overview[/bold]\n\n"
        f"Total Nodes: {stats['total_nodes']}\n"
        f"  Tables: {stats['node_type_counts']['table']}\n"
        f"  Columns: {stats['node_type_counts']['column']}\n"
        f"  Concepts: {stats['node_type_counts']['concept']}\n"
        f"  Entities: {stats['node_type_counts']['entity']}\n\n"
        f"Total Edges: {stats['total_edges']}\n"
        f"  Contains: {stats['edge_type_counts']['contains']}\n"
        f"  FK: {stats['edge_type_counts']['foreign_key']}\n"
        f"  Joinable: {stats['edge_type_counts']['joinable']}\n"
        f"  Synonyms: {stats['edge_type_counts']['semantic_synonym']}\n"
        f"  Distribution: {stats['edge_type_counts']['distribution_similar']}\n"
        f"  Correlated: {stats['edge_type_counts']['correlated']}\n"
        f"  Represents: {stats['edge_type_counts']['represents']}\n\n"
        f"Pending Edges: {stats['pending_edge_count']}\n"
        f"  Pending FK: {stats['pending_edge_type_counts']['foreign_key']}\n"
        f"  Pending Joinable: {stats['pending_edge_type_counts']['joinable']}\n\n"
        f"Profiles: {stats['total_profiles']}\n"
        f"DB Path: {db_path}",
        title="DataLink",
        border_style="blue",
    )
    console.print(panel)
    storage.close()


@app.command()
def path(
    from_node: str = typer.Option(..., "--from", help="Source node ID"),
    to_node: str = typer.Option(..., "--to", help="Target node ID"),
    max_depth: int = typer.Option(3, "--depth", "-d", help="Maximum path depth"),
    limit: int = typer.Option(3, "--limit", "-l", help="Maximum number of paths to return"),
    edge_types: str = typer.Option(
        "", "--edge-types", "-e", help="Comma-separated edge types to traverse (e.g., joinable,foreign_key)"
    ),
    db_path: str = typer.Option("datalink.db", "--db", help="Path to graph database file"),
) -> None:
    """Find paths between two nodes in the data graph."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage
    from datalink.models.edge import EdgeType

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    et = None
    if edge_types:
        try:
            type_names = [t.strip() for t in edge_types.split(",")]
            et = [EdgeType(t) for t in type_names]
        except ValueError as exc:
            console.print(f"[bold red]Invalid edge type: {exc}[/bold red]")
            raise typer.Exit(code=1)

    paths = retrieval.find_paths(from_node, to_node, max_depth, et, limit=limit, mask_credential=False)

    if not paths:
        console.print(f"[yellow]No paths found between '{from_node}' and '{to_node}'[/yellow]")
        return

    for i, p in enumerate(paths):
        console.print(f"\n[bold]Path {i + 1}[/bold] (confidence: {p['confidence']:.3f}, length: {p['length']})")

        for j, node_id in enumerate(p["nodes"]):
            node = storage.get_node(node_id)
            if node:
                prefix = "  → " if j > 0 else "  "
                console.print(f"{prefix}[cyan]{node.type.value}[/cyan]: {node.name} ({node_id})")

        console.print("  Edges:")
        for e in p["edges"]:
            console.print(f"    [yellow]{e['type']}[/yellow] (confidence: {e['confidence']:.2f})")

    storage.close()


@app.command()
def pending_edges(
    node_id: str = typer.Option("", "--node", "-n", help="Filter by node ID"),
    edge_type: str = typer.Option("", "--type", "-t", help="Filter by edge type (e.g., foreign_key, joinable)"),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum number of results"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """List pending (dangling) edges that reference nodes not yet in the graph."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage
    from datalink.models.edge import EdgeType

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    et = None
    if edge_type:
        try:
            et = EdgeType(edge_type)
        except ValueError:
            console.print(f"[bold red]Invalid edge type: {edge_type}[/bold red]")
            raise typer.Exit(code=1)

    nid = node_id if node_id else None
    results = retrieval.get_pending_edges(nid, et, limit, mask_credential=False)

    if not results:
        console.print("[green]No pending edges found — all edges are resolved.[/green]")
        storage.close()
        return

    table = Table(title="Pending (Dangling) Edges", show_lines=True)
    table.add_column("Type", style="blue")
    table.add_column("Missing", style="red")
    table.add_column("Note", style="magenta", max_width=40)
    table.add_column("Source", style="yellow", max_width=50, overflow="ellipsis", no_wrap=True)
    table.add_column("Target", style="yellow", max_width=50, overflow="ellipsis", no_wrap=True)
    table.add_column("ID", style="cyan", max_width=40, overflow="ellipsis", no_wrap=True)

    for r in results:
        table.add_row(
            r["type"],
            ",".join(r["missing_endpoints"]),
            r["note"],
            r["source_id"],
            r["target_id"],
            r["id"],
        )

    console.print(table)
    storage.close()


@app.command()
def list_datasets(
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """List all tables/datasets in the data graph with basic statistics."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    datasets = retrieval.list_datasets(mask_credential=False)

    if not datasets:
        console.print("[yellow]No datasets found[/yellow]")
        storage.close()
        return

    table = Table(title="Datasets", show_lines=True)
    table.add_column("Name", style="green")
    table.add_column("Columns", style="blue")
    table.add_column("Edges", style="yellow")
    table.add_column("ID", style="cyan", max_width=60, overflow="ellipsis", no_wrap=True)

    for ds in datasets:
        table.add_row(ds["name"], str(ds["column_count"]), str(ds["inferred_edge_count"]), ds["id"])

    console.print(table)
    storage.close()


@app.command()
def get_node(
    node_id: str = typer.Argument(..., help="Node ID to retrieve"),
    include_edges: bool = typer.Option(True, "--edges/--no-edges", help="Include adjacent edges"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Get detailed information about a specific node."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    result = retrieval.get_node(node_id, include_edges, mask_credential=False)

    if result is None:
        console.print(f"[yellow]Node '{node_id}' not found[/yellow]")
        storage.close()
        raise typer.Exit(code=1)

    # Display node info
    console.print(f"[bold cyan]{result['type']}[/bold cyan]: {result['name']} ({result['id']})")

    if "profile" in result:
        console.print("\n[bold]Profile:[/bold]")
        profile = result["profile"]
        for key, value in profile.items():
            if key in ("id", "column_id"):
                continue
            console.print(f"  {key}: {value}")

    if include_edges and "edges" in result:
        console.print(f"\n[bold]Edges ({len(result['edges'])}):[/bold]")
        for e in result["edges"]:
            # Show direction and the "other" node, not just target_id.
            other = e.get("other_node")
            if other:
                other_label = f"{other['type']}: {other['name']}"
            else:
                other_label = e["target_id"] if e["direction"] == "outgoing" else e["source_id"]
            arrow = "→" if e["direction"] == "outgoing" else "←"
            console.print(f"  [yellow]{e['type']}[/yellow] {arrow} {other_label} (confidence: {e['confidence']:.2f})")

    storage.close()


@app.command()
def extract_subgraph(
    node_ids: str = typer.Argument(..., help="Comma-separated node IDs to start from"),
    max_hops: int = typer.Option(2, "--hops", "-h", help="Number of neighbor layers to expand"),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
) -> None:
    """Extract a subgraph around specified nodes, expanding by neighbor hops."""
    from datalink.graph.retrieval import GraphRetrieval
    from datalink.graph.storage import GraphStorage

    storage = GraphStorage(db_path)
    retrieval = GraphRetrieval(storage)

    ids = [id.strip() for id in node_ids.split(",")]
    result = retrieval.extract_subgraph(ids, max_hops, mask_credential=False)

    stats = result["stats"]
    console.print(f"[bold]Subgraph:[/bold] {stats['node_count']} nodes, {stats['edge_count']} edges")

    # Display nodes
    table = Table(title="Subgraph Nodes", show_lines=True)
    table.add_column("Name", style="green")
    table.add_column("Type", style="blue")
    table.add_column("ID", style="cyan", max_width=60, overflow="ellipsis", no_wrap=True)

    for n in result["nodes"]:
        table.add_row(n["name"], n["type"], n["id"])

    console.print(table)

    # Display edges
    if result["edges"]:
        edge_table = Table(title="Subgraph Edges", show_lines=True)
        edge_table.add_column("Type", style="blue")
        edge_table.add_column("Conf", style="magenta")
        edge_table.add_column("Source", style="yellow", max_width=50, overflow="ellipsis", no_wrap=True)
        edge_table.add_column("Target", style="yellow", max_width=50, overflow="ellipsis", no_wrap=True)

        for e in result["edges"]:
            edge_table.add_row(e["type"], f"{e['confidence']:.2f}", e["source_id"], e["target_id"])

        console.print(edge_table)

    storage.close()


# ── Service & config ───────────────────────────────────────────────────


@app.command()
def serve(
    port: int = typer.Option(8080, "--port", "-p", help="Port for MCP server"),
    host: str = typer.Option(
        "", "--host", help="Bind address (empty=0.0.0.0 for all interfaces, 127.0.0.1 for localhost-only)"
    ),
    db_path: str = typer.Option("datalink.db", "--db", "-d", help="Path to graph database file"),
    transport: str = typer.Option(
        "sse", "--transport", "-t", help="Transport protocol: 'sse' (legacy) or 'streamable-http' (recommended)"
    ),
) -> None:
    """Start the DataLink MCP server for agent integration."""
    if transport not in ("sse", "streamable-http"):
        console.print(f"[bold red]Invalid transport: {transport}. Use 'sse' or 'streamable-http'.[/bold red]")
        raise typer.Exit(code=1)

    effective_host = host or "0.0.0.0"
    console.print(
        f"[bold blue]Starting DataLink MCP server on {effective_host}:{port} (transport={transport})...[/bold blue]"
    )

    from datalink.mcp.server import start_mcp_server

    start_mcp_server(db_path=db_path, port=port, transport=transport, host=host)


@app.command()
def api(
    port: int = typer.Option(8081, "--port", "-p", help="Port for REST API server"),
    host: str = typer.Option(
        "0.0.0.0", "--host", help="Bind address (default 0.0.0.0 for all interfaces, 127.0.0.1 for localhost-only)"
    ),
) -> None:
    """Start the DataLink REST API server (mirrors all CLI commands as HTTP endpoints)."""
    console.print(f"[bold blue]Starting DataLink REST API server on {host}:{port}...[/bold blue]")

    from datalink.api.server import start_api_server

    start_api_server(host=host, port=port)


@app.command()
def config(
    llm_model: str = typer.Option("", "--llm-model", help="LLM model name"),
    llm_api_key: str = typer.Option("", "--llm-api-key", help="LLM API key"),
    llm_base_url: str = typer.Option("", "--llm-base-url", help="LLM API base URL (OpenAI-compatible)"),
    db_path: str = typer.Option("", "--db", "-d", help="Default graph database path"),
    mcp_tools: str = typer.Option(
        "",
        "--mcp-tools",
        "-t",
        help="Auxiliary MCP tools to expose (comma-separated, e.g. datalink_search_nodes,datalink_get_node)",
    ),
) -> None:
    """Configure DataLink settings."""
    current = DataLinkConfig.load()

    if llm_model:
        current.llm.model = llm_model
    if llm_api_key:
        current.llm.api_key = llm_api_key
    if llm_base_url:
        current.llm.base_url = llm_base_url
    if db_path:
        current.graph_db_path = db_path
    if mcp_tools:
        current.mcp_tools = mcp_tools

    current.save()

    console.print("[bold green]Configuration saved![/bold green]")
    console.print(f"  LLM Model: {current.llm.model}")
    console.print(f"  LLM Base URL: {current.llm.base_url}")
    console.print(f"  DB Path: {current.graph_db_path}")
    console.print(f"  MCP Tools: {current.mcp_tools or '(none — only core tools)'}")


if __name__ == "__main__":
    app()
