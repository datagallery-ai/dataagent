"""Build pipeline orchestration — init, rebuild, add_datasource, remove_table."""

import logging
from typing import Any

from datalink.config import DataLinkConfig
from datalink.connector.database import DatabaseConnector
from datalink.connector.file import FileConnector
from datalink.extractor.tabular import TabularExtractor
from datalink.graph.storage import GraphStorage
from datalink.inferrer.correlated import CorrelationInferrer
from datalink.inferrer.distribution import DistributionInferrer
from datalink.inferrer.joinable import JoinableInferrer
from datalink.inferrer.synonym import SynonymInferrer
from datalink.mapper.llm_mapper import LLMMapper
from datalink.models.datasource import DatasourceConfig, DatasourceInfo, DatasourceType
from datalink.models.edge import Edge, EdgeType, PendingEdge
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, Node, NodeType, TableNode
from datalink.models.profile import ColumnProfile
from datalink.profiler.tabular import TabularProfiler
from datalink.utils.embedding import EmbeddingService, node_to_searchable_text

logger = logging.getLogger(__name__)


def _connect_datasource(config: DatasourceConfig) -> DatasourceInfo:
    """Connect to a datasource and extract metadata + sample data."""
    if config.type == DatasourceType.DATABASE:
        connector = DatabaseConnector(config)
    elif config.type in (DatasourceType.CSV, DatasourceType.PARQUET):
        connector = FileConnector(config)
    else:
        raise ValueError(f"Unsupported datasource type: {config.type}")

    connector.connect()
    ds_info = connector.get_datasource_info()
    connector.disconnect()
    return ds_info


def _detect_datasource_type_from_source(source: str) -> DatasourceType:
    """Auto-detect datasource type from a source string.

    Used by rebuild to infer the type of stored source paths.
    """
    if source.startswith(("postgresql://", "mysql://", "sqlite://", "mssql://", "oracle://")):
        return DatasourceType.DATABASE
    elif source.endswith(".parquet") or source.endswith(".pq"):
        return DatasourceType.PARQUET
    else:
        # Default to CSV for file paths / directories
        return DatasourceType.CSV


def _build_datasource_config_from_table(table: TableNode) -> DatasourceConfig:
    """Reconstruct a DatasourceConfig from a stored TableNode's metadata.

    Uses source_type if stored, otherwise auto-detects from the source string.
    """
    source_type_str = table.source_type
    if not source_type_str:
        source_type_str = table.properties.get("source_type", "")
    if not source_type_str:
        source_type_str = _detect_datasource_type_from_source(table.source).value

    ds_type = DatasourceType(source_type_str)

    return DatasourceConfig(
        type=ds_type,
        path=table.source if ds_type in (DatasourceType.CSV, DatasourceType.PARQUET) else "",
        connection_string=table.source if ds_type == DatasourceType.DATABASE else "",
    )


class BuildPipeline:
    """Orchestrate the graph build pipeline.

    Build modes:
    - init_build: first-time build, user provides datasource configs
    - rebuild: refresh graph from existing table metadata
      - mode=full: complete rebuild (clear + re-pipeline + re-embed)
      - mode=vec: re-build embedding vectors only (no data change)
      - mode=profile: re-compute statistics only (no semantic layer)
    - add_datasource: incremental add from a new datasource
    """

    def __init__(self, config: DataLinkConfig):
        """Initialize the build pipeline with configuration.

        Args:
            config: DataLink configuration including LLM settings.
        """
        self.config = config
        self.storage = GraphStorage(config.graph_db_path)

        # Initialize embedding service (optional — disabled when model is empty)
        self.embedding_service: EmbeddingService | None = None
        if config.embedding.is_available(config.llm):
            self.embedding_service = EmbeddingService(config.embedding, config.llm)

    # ── Init build (first-time) ─────────────────────────────────────────

    def init_build(self, datasource_configs: list[DatasourceConfig]) -> dict[str, Any]:
        """First-time build: construct the entire graph from scratch.

        Clears any existing data, then processes all provided datasources
        through the full pipeline. Used when the graph is empty or the user
        wants to start fresh with specific datasources.

        Args:
            datasource_configs: List of datasource configurations to build from.

        Returns:
            Dict with build results and statistics.
        """
        logger.info(f"Starting init build with {len(datasource_configs)} datasources")

        # Clear any existing data
        self.storage.clear_all()

        # Delegate to the shared pipeline logic
        all_ds_infos = self._connect_all(datasource_configs)
        result = self._compute_pipeline(all_ds_infos)
        self._store_pipeline_result(result)

        # Store build metadata
        self.storage.set_metadata("build_type", "init")
        self.storage.set_metadata("build_time", str(len(datasource_configs)))

        # Build embedding vectors (if configured)
        self._build_embeddings_from_pipeline_result(result)

        stats = self.storage.get_graph_stats()
        logger.info(f"Init build complete: {stats}")

        return {
            "status": "success",
            "datasources": len(datasource_configs),
            "tables": len(result["tables"]),
            "stats": stats,
        }

    # ── Rebuild (refresh from existing metadata) ────────────────────────

    def rebuild(self, mode: str = "full") -> dict[str, Any]:
        """Rebuild the graph from existing data sources.

        Three rebuild modes:

        - mode="full": Clear the graph and re-run the entire pipeline
          (extract → profile → infer → map → embed). This is the original
          rebuild behavior, now also includes embedding vector construction.

        - mode="vec": Re-build embedding vectors only using the current
          embedding model. No data changes, no LLM calls — just re-embeds
          all existing nodes' searchable text. Useful when the user changes
          the embedding model configuration.

        - mode="profile": Re-compute column/table statistics from the
          original data sources, without touching the semantic layer or
          making any LLM calls. Useful when data has changed but the
          semantic structure should remain stable.

        **Safety guarantee (mode=full)**: old data is only cleared AFTER
        the new pipeline succeeds. If the pipeline fails, the old graph
        is preserved.

        Args:
            mode: Rebuild mode — "full", "vec", or "profile".

        Returns:
            Dict with rebuild results and statistics.
        """
        if mode == "vec":
            return self._rebuild_vec()
        elif mode == "profile":
            return self._rebuild_profile()
        elif mode == "full":
            return self._rebuild_full()
        else:
            return {
                "status": "error",
                "error": f"Invalid rebuild mode: '{mode}'. Valid modes: full, vec, profile.",
                "stats": self.storage.get_graph_stats(),
            }

    def _rebuild_full(self) -> dict[str, Any]:
        """Full rebuild: clear and re-run the entire pipeline.

        This is the original rebuild behavior, now also includes embedding
        vector construction.
        """
        logger.info("Starting full rebuild from existing table metadata")

        # Read all existing TableNodes BEFORE any modification
        existing_tables = self.storage.get_nodes_by_type(NodeType.TABLE)
        table_nodes = [t for t in existing_tables if isinstance(t, TableNode)]

        if not table_nodes:
            logger.warning("No tables found in the graph — nothing to rebuild")
            return {
                "status": "error",
                "error": "No tables found in the graph. Use init-build or add-source first.",
                "stats": self.storage.get_graph_stats(),
            }

        # Group tables by source (one DB connection may cover many tables)
        source_groups: dict[str, list[TableNode]] = {}
        for table in table_nodes:
            source = table.source or table.properties.get("source", "")
            if not source:
                logger.warning(f"Table '{table.name}' (id={table.id}) has no source metadata — skipping")
                continue
            if source not in source_groups:
                source_groups[source] = []
            source_groups[source].append(table)

        # Reconstruct DatasourceConfigs and connect
        datasource_configs: list[DatasourceConfig] = []
        all_ds_infos: list[DatasourceInfo] = []

        for source, tables_in_group in source_groups.items():
            # Use the first table's metadata to build the config
            # (all tables in the same source share the same connection)
            ds_config = _build_datasource_config_from_table(tables_in_group[0])
            datasource_configs.append(ds_config)

            ds_info = _connect_datasource(ds_config)

            # Filter to only the tables that were in the original graph
            existing_table_names = {t.name for t in tables_in_group}
            ds_info.tables = [t for t in ds_info.tables if t.name in existing_table_names]
            ds_info.sample_data = {
                name: rows for name, rows in ds_info.sample_data.items() if name in existing_table_names
            }

            all_ds_infos.append(ds_info)

        # Run the full pipeline WITHOUT storing to DB (compute-only).
        # This collects all artifacts first. If it fails, old data is untouched.
        try:
            result = self._compute_pipeline(all_ds_infos)
        except Exception as e:
            logger.error(f"Rebuild pipeline failed: {e} — old graph preserved")
            return {
                "status": "error",
                "error": f"Rebuild pipeline failed: {e}. Old graph data is preserved — retry rebuild to recover.",
                "stats": self.storage.get_graph_stats(),
            }

        # Pipeline succeeded — clear old data and store new results
        self.storage.clear_all()
        self._store_pipeline_result(result)

        # Store build metadata
        self.storage.set_metadata("build_type", "rebuild")
        self.storage.set_metadata("build_time", str(len(datasource_configs)))

        # Build embedding vectors (if configured)
        self._build_embeddings_from_pipeline_result(result)

        stats = self.storage.get_graph_stats()
        logger.info(f"Full rebuild complete: {stats}")

        return {
            "status": "success",
            "datasources": len(datasource_configs),
            "tables": len(result["tables"]),
            "stats": stats,
        }

    # ── Add datasource (incremental) ────────────────────────────────────

    def add_datasource(
        self,
        datasource_config: DatasourceConfig,
        table_names: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add tables from a datasource to the existing graph.

        Args:
            datasource_config: Configuration for the datasource.
            table_names: Optional list of table names to add.
                None → add all tables from this datasource.
                ["orders", "products"] → add only these tables.

        Returns:
            Dict with add results and statistics.
        """
        ds_info = _connect_datasource(datasource_config)

        # Filter to specific tables if requested
        if table_names:
            requested_set = set(table_names)
            ds_info.tables = [t for t in ds_info.tables if t.name in requested_set]
            ds_info.sample_data = {name: rows for name, rows in ds_info.sample_data.items() if name in requested_set}
            if not ds_info.tables:
                available = [t.name for t in _connect_datasource(datasource_config).tables]
                raise ValueError(
                    f"None of the requested tables {table_names} found in datasource. Available: {available}"
                )

        # Deduplicate: skip tables that already exist in the graph.
        # Re-adding a table without first removing it would leave stale
        # edges, profiles, and concept mappings behind (INSERT OR REPLACE
        # only overwrites the node itself, not its associated data).
        existing_tables = self.storage.get_nodes_by_type(NodeType.TABLE)
        existing_table_ids = {n.id for n in existing_tables if isinstance(n, TableNode)}

        # Build the expected IDs for each candidate table
        # (ID format: "table:{source}:{table_name}")
        new_tables_to_add = []
        skipped_tables = []
        for table_info in ds_info.tables:
            source_name = (
                table_info.source or datasource_config.path or datasource_config.connection_string or "unknown"
            )
            from datalink.extractor.tabular import generate_id

            expected_id = generate_id("table", source_name, table_info.name)

            if expected_id in existing_table_ids:
                skipped_tables.append(table_info.name)
                logger.info(f"Table '{table_info.name}' (id={expected_id}) already exists in graph — skipping")
            else:
                new_tables_to_add.append(table_info.name)

        # Filter ds_info to only the new tables
        if skipped_tables:
            ds_info.tables = [t for t in ds_info.tables if t.name in set(new_tables_to_add)]
            ds_info.sample_data = {
                name: rows for name, rows in ds_info.sample_data.items() if name in set(new_tables_to_add)
            }
            logger.info(f"Skipped {len(skipped_tables)} existing tables: {skipped_tables}")

        if not ds_info.tables:
            logger.warning("All requested tables already exist in the graph — nothing to add")
            stats = self.storage.get_graph_stats()
            return {
                "status": "skipped",
                "added_tables": [],
                "skipped_tables": skipped_tables,
                "message": "All tables already exist in the graph. Use remove_table first if you want to re-add.",
                "stats": stats,
            }

        added_table_names = [t.name for t in ds_info.tables]
        logger.info(f"Adding {len(added_table_names)} new tables: {added_table_names}")

        # Delegate to the shared pipeline logic
        self._add_tables_pipeline(ds_info)

        # Build embedding vectors for newly added nodes (if configured)
        self._build_embeddings_incremental()

        stats = self.storage.get_graph_stats()
        logger.info(f"Datasource added: {stats}")

        return {
            "status": "success",
            "added_tables": added_table_names,
            "skipped_tables": skipped_tables,
            "stats": stats,
        }

    def add_table(self, datasource_config: DatasourceConfig, table_name: str) -> dict[str, Any]:
        """Add a single table to the existing graph.

        Convenience wrapper around add_datasource for adding one table.

        Args:
            datasource_config: Configuration for the datasource.
            table_name: Name of the single table to add.

        Returns:
            Dict with add results and statistics.
        """
        return self.add_datasource(datasource_config, [table_name])

    # ── Remove table ────────────────────────────────────────────────────

    def remove_table(self, table_id: str, cleanup_orphans: bool = True) -> dict[str, Any]:
        """Remove a table and all its columns from the graph.

        Args:
            table_id: ID of the TableNode to remove.
            cleanup_orphans: Whether to remove orphaned Concept/Entity nodes.

        Returns:
            Dict with removal results.
        """
        logger.info(f"Removing table '{table_id}'")

        removed_column_ids = self.storage.remove_table(table_id)

        # Clean up pending edges referencing removed nodes
        removed_ids = set(removed_column_ids + [table_id])
        self.storage.cleanup_pending_edges_for_removed_nodes(removed_ids)

        # Optionally clean up orphaned semantic nodes
        orphan_count = 0
        if cleanup_orphans:
            orphan_count = self.storage.cleanup_orphaned_semantic_nodes()

        stats = self.storage.get_graph_stats()
        logger.info(f"Table removed: {stats}")

        return {
            "status": "success",
            "removed_columns": len(removed_column_ids),
            "removed_orphans": orphan_count,
            "stats": stats,
        }

    # ── Close ───────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the storage connection."""
        self.storage.close()

    # ── Shared pipeline logic ───────────────────────────────────────────

    def _connect_all(self, datasource_configs: list[DatasourceConfig]) -> list[DatasourceInfo]:
        """Connect to all datasources and extract metadata."""
        all_ds_infos: list[DatasourceInfo] = []
        for ds_config in datasource_configs:
            ds_info = _connect_datasource(ds_config)
            all_ds_infos.append(ds_info)
        return all_ds_infos

    def _compute_pipeline(self, all_ds_infos: list[DatasourceInfo]) -> dict[str, Any]:
        """Run the full pipeline on all datasource infos (compute-only, no DB writes).

        Steps 2-5: Extract → Profile → Infer → Map.
        Returns all artifacts so the caller can decide when to store them.

        Returns:
            Dict with all pipeline artifacts:
            - tables, columns, profiles (structural)
            - concepts, entities, semantic_edges (semantic)
            - structural_edges, inferred_edges (structural + inferred)
        """
        # Step 2: Extract structural nodes
        extractor = TabularExtractor()
        all_tables: list[TableNode] = []
        all_columns: list[ColumnNode] = []
        all_structural_edges: list[Edge] = []

        for ds_info in all_ds_infos:
            tables, columns, edges = extractor.extract(ds_info)
            all_tables.extend(tables)
            all_columns.extend(columns)
            all_structural_edges.extend(edges)

        logger.info(f"Extracted {len(all_tables)} tables, {len(all_columns)} columns")

        # Step 3: Profile columns
        profiler = TabularProfiler()
        all_profiles: list[ColumnProfile] = []

        for ds_info in all_ds_infos:
            profiles = profiler.profile_datasource(ds_info)
            all_profiles.extend(profiles)

        self._update_column_properties(all_columns, all_profiles)
        self._update_table_properties(all_tables)

        logger.info(f"Profiled {len(all_profiles)} columns")

        # Step 4: Infer implicit relationships
        joinable_edges = JoinableInferrer(overlap_threshold=self.config.joinable_overlap_threshold).infer(
            all_columns, all_profiles
        )

        synonym_edges = SynonymInferrer().infer(all_columns, all_profiles)

        distribution_edges = DistributionInferrer().infer(all_columns, all_profiles)

        correlated_edges = CorrelationInferrer(correlation_threshold=self.config.correlation_threshold).infer(
            all_columns, all_profiles, joinable_edges, all_ds_infos
        )

        all_inferred_edges = joinable_edges + synonym_edges + distribution_edges + correlated_edges

        logger.info(
            f"Inferred {len(all_inferred_edges)} edges "
            f"(joinable={len(joinable_edges)}, synonym={len(synonym_edges)}, "
            f"distribution={len(distribution_edges)}, "
            f"correlated={len(correlated_edges)})"
        )

        # Step 5: Map to semantic layer
        llm_mapper = LLMMapper(self.config)
        all_concepts, all_entities, all_semantic_edges = llm_mapper.map_columns(all_columns, all_profiles)

        logger.info(
            f"Semantic mapping: {len(all_concepts)} concepts, "
            f"{len(all_entities)} entities, "
            f"{len(all_semantic_edges)} edges"
        )

        # Step 5b: Generate table comments for tables lacking SQL metadata
        table_comments = llm_mapper.generate_table_comments(
            all_tables,
            all_columns,
            all_profiles,
            all_concepts,
            all_entities,
            all_semantic_edges,
        )
        if table_comments:
            logger.info(f"Generated LLM comments for {len(table_comments)} tables")
            # Re-run _update_table_properties to persist the new comments
            self._update_table_properties(all_tables)

        # Return ALL artifacts — the caller handles DB storage
        return {
            "tables": all_tables,
            "columns": all_columns,
            "profiles": all_profiles,
            "concepts": all_concepts,
            "entities": all_entities,
            "structural_edges": all_structural_edges,
            "inferred_edges": all_inferred_edges,
            "semantic_edges": all_semantic_edges,
        }

    def _store_pipeline_result(self, result: dict[str, Any]) -> None:
        """Store pipeline artifacts into the graph database.

        Separated from _compute_pipeline so callers can control
        when data gets written (e.g. rebuild clears old data first).
        """
        all_nodes = result["tables"] + result["columns"] + result["concepts"] + result["entities"]
        self.storage.add_nodes_batch(all_nodes)

        all_edges = result["structural_edges"] + result["inferred_edges"] + result["semantic_edges"]
        self._store_edges(all_edges, {n.id for n in all_nodes})

        self.storage.add_profiles_batch(result["profiles"])

    def _add_tables_pipeline(self, ds_info: DatasourceInfo) -> dict[str, Any]:
        """Run the incremental pipeline for adding new tables.

        Same steps as full pipeline, but:
        - Infers on combined (existing + new) columns/profiles
        - Only keeps edges involving new columns
        - No CorrelationInferrer (missing old sample data)
        - Semantic mapping with merge_with_existing
        """
        # Step 2: Extract
        extractor = TabularExtractor()
        new_tables, new_columns, structural_edges = extractor.extract(ds_info)

        # Step 3: Profile
        profiler = TabularProfiler()
        new_profiles = profiler.profile_datasource(ds_info)

        self._update_column_properties(new_columns, new_profiles)
        self._update_table_properties(new_tables)

        # Step 4: Infer on combined set
        existing_column_nodes = [
            n for n in self.storage.get_nodes_by_type(NodeType.COLUMN) if isinstance(n, ColumnNode)
        ]
        existing_profiles = [
            p for col in existing_column_nodes if (p := self.storage.get_profile_for_column(col.id)) is not None
        ]

        all_columns_combined = existing_column_nodes + new_columns
        all_profiles_combined = existing_profiles + new_profiles

        new_joinable = JoinableInferrer(overlap_threshold=self.config.joinable_overlap_threshold).infer(
            all_columns_combined, all_profiles_combined
        )

        new_synonym = SynonymInferrer().infer(all_columns_combined, all_profiles_combined)

        new_distribution = DistributionInferrer().infer(all_columns_combined, all_profiles_combined)

        # Only keep edges involving at least one new column
        new_col_ids = {c.id for c in new_columns}

        def involves_new(edge: Edge) -> bool:
            return edge.source_id in new_col_ids or edge.target_id in new_col_ids

        new_joinable = [e for e in new_joinable if involves_new(e)]
        new_synonym = [e for e in new_synonym if involves_new(e)]
        new_distribution = [e for e in new_distribution if involves_new(e)]

        # Step 5: Semantic mapping + merge
        llm_mapper = LLMMapper(self.config)
        new_concepts, new_entities, new_semantic = llm_mapper.map_columns(new_columns, new_profiles)

        # Step 5b: Generate table comments for new tables lacking SQL metadata
        table_comments = llm_mapper.generate_table_comments(
            new_tables,
            new_columns,
            new_profiles,
            new_concepts,
            new_entities,
            new_semantic,
        )
        if table_comments:
            logger.info(f"Generated LLM comments for {len(table_comments)} new tables")
            self._update_table_properties(new_tables)

        if new_concepts or new_entities:
            existing_concepts = [
                n for n in self.storage.get_nodes_by_type(NodeType.CONCEPT) if isinstance(n, ConceptNode)
            ]
            existing_entities = [
                n for n in self.storage.get_nodes_by_type(NodeType.ENTITY) if isinstance(n, EntityNode)
            ]

            # Only merge if there are actual existing nodes to merge with.
            # If the graph has no concepts/entities yet (first build on empty
            # graph), map_columns already did self-merge internally, so
            # calling merge_with_existing([], []) would trigger a redundant
            # self-merge — wasting an LLM call for no benefit.
            if existing_concepts or existing_entities:
                new_concepts, new_entities, new_semantic = llm_mapper.merge_with_existing(
                    new_concepts,
                    new_entities,
                    new_semantic,
                    existing_concepts,
                    existing_entities,
                )

        # Step 6: Store
        # new_concepts/new_entities now include both genuinely new nodes
        # and enriched existing nodes (from merge_with_existing).
        # INSERT OR REPLACE ensures enriched existing nodes are updated.
        all_nodes = new_tables + new_columns + new_concepts + new_entities
        self.storage.add_nodes_batch(all_nodes)

        new_edges = structural_edges + new_joinable + new_synonym + new_distribution + new_semantic

        # Collect all node IDs that now exist in the graph
        available_node_ids = set(self.storage.get_all_node_ids())
        available_node_ids.update({n.id for n in all_nodes})

        self._store_edges(new_edges, available_node_ids)

        # Resolve previously pending edges that now have both endpoints
        resolved = self.storage.resolve_pending_edges(available_node_ids)
        logger.info(f"Resolved {resolved} previously pending edges")

        self.storage.add_profiles_batch(new_profiles)

        return {
            "tables": new_tables,
            "columns": new_columns,
        }

    # ── Rebuild modes: vec and profile ───────────────────────────────────

    def _rebuild_vec(self) -> dict[str, Any]:
        """Re-build embedding vectors only using the current embedding model.

        No data changes, no LLM calls — just re-embeds all existing nodes'
        searchable text. Useful when the user changes the embedding model
        configuration.
        """
        logger.info("Starting vec rebuild — re-building embedding vectors")

        # Check embedding availability
        if not self.embedding_service or not self.embedding_service.is_available():
            return {
                "status": "error",
                "error": "Embedding model not configured or API key unavailable. "
                "Set embedding.model and embedding.api_key in datalink_config.json first.",
                "stats": self.storage.get_graph_stats(),
            }

        # Collect all nodes + profiles
        nodes_and_profiles = self._collect_nodes_with_profiles()
        if not nodes_and_profiles:
            logger.warning("No nodes found — nothing to embed")
            return {
                "status": "error",
                "error": "No nodes in the graph. Use add-table first.",
                "stats": self.storage.get_graph_stats(),
            }

        # Generate searchable text for each node
        texts = []
        for node, profile in nodes_and_profiles:
            if isinstance(node, TableNode):
                col_names = self._get_column_names_for_table(node)
                texts.append(node_to_searchable_text(node, profile, column_names=col_names))
            else:
                texts.append(node_to_searchable_text(node, profile))

        # Compute embeddings
        model_name = self.embedding_service.get_model_name()
        logger.info(f"Computing {len(texts)} embeddings with model {model_name}")

        embeddings = self.embedding_service.compute_embeddings(texts)
        if not embeddings or len(embeddings) != len(texts):
            logger.error("Embedding computation failed or returned incomplete results")
            return {
                "status": "error",
                "error": "Embedding computation failed. Check API connectivity.",
                "stats": self.storage.get_graph_stats(),
            }

        # Clear old embeddings and store new ones
        self.storage.clear_embeddings()
        data = [(node.id, model_name, embeddings[i], texts[i]) for i, (node, profile) in enumerate(nodes_and_profiles)]
        self.storage.add_embeddings_batch(data)

        # Record the model in metadata
        self.storage.set_metadata("embedding_model", model_name)

        stats = self.storage.get_graph_stats()
        logger.info(f"Vec rebuild complete: {len(data)} vectors built with model {model_name}")

        return {
            "status": "success",
            "vectors_built": len(data),
            "model": model_name,
            "stats": stats,
        }

    def _rebuild_profile(self) -> dict[str, Any]:
        """Re-compute column/table statistics and profile-dependent inferred edges.

        Reconnects to each data source and re-runs the Profiler step, then
        re-infers all edges that depend on profile statistics (JOINABLE,
        DISTRIBUTION_SIMILAR, SEMANTIC_SYNONYM, CORRELATED). No LLM calls,
        no semantic mapping — the semantic layer (REPRESENTS, HAS_CONCEPT)
        and structural edges (CONTAINS, FOREIGN_KEY) are left untouched.

        Useful when data has changed but the semantic structure should remain stable.
        """
        logger.info("Starting profile rebuild — re-computing statistics + inferred edges")

        # Read all existing TableNodes
        existing_tables = self.storage.get_nodes_by_type(NodeType.TABLE)
        table_nodes = [t for t in existing_tables if isinstance(t, TableNode)]

        if not table_nodes:
            logger.warning("No tables found in the graph — nothing to rebuild")
            return {
                "status": "error",
                "error": "No tables found in the graph. Use add-table first.",
                "stats": self.storage.get_graph_stats(),
            }

        # Group tables by source
        source_groups: dict[str, list[TableNode]] = {}
        for table in table_nodes:
            source = table.source or table.properties.get("source", "")
            if not source:
                logger.warning(f"Table '{table.name}' has no source metadata — skipping")
                continue
            if source not in source_groups:
                source_groups[source] = []
            source_groups[source].append(table)

        # Re-connect and collect DatasourceInfos for all sources
        profiler = TabularProfiler()
        updated_profiles: list[ColumnProfile] = []
        all_ds_infos: list[DatasourceInfo] = []

        for source, tables_in_group in source_groups.items():
            ds_config = _build_datasource_config_from_table(tables_in_group[0])
            try:
                ds_info = _connect_datasource(ds_config)
                existing_table_names = {t.name for t in tables_in_group}
                ds_info.tables = [t for t in ds_info.tables if t.name in existing_table_names]
                ds_info.sample_data = {
                    name: rows for name, rows in ds_info.sample_data.items() if name in existing_table_names
                }

                profiles = profiler.profile_datasource(ds_info)
                updated_profiles.extend(profiles)
                all_ds_infos.append(ds_info)
                logger.info(f"Updated {len(profiles)} profiles for source '{source}'")
            except Exception as e:
                logger.warning(f"Failed to reconnect to source '{source}': {e} — skipping")

        if not updated_profiles:
            stats = self.storage.get_graph_stats()
            return {
                "status": "error",
                "error": "No profiles could be updated. Check data source connectivity.",
                "stats": stats,
            }

        # Update profiles in DB (INSERT OR REPLACE)
        self.storage.add_profiles_batch(updated_profiles)

        # Update column node properties (semantic_type, dtype, etc.)
        column_nodes = [n for n in self.storage.get_nodes_by_type(NodeType.COLUMN) if isinstance(n, ColumnNode)]
        self._update_column_properties(column_nodes, updated_profiles)

        # Update table properties (row_count, column_ids)
        self._update_table_properties(table_nodes)
        # Re-store updated nodes
        all_nodes_to_update = column_nodes + table_nodes
        self.storage.add_nodes_batch(all_nodes_to_update)

        # ── Re-infer profile-dependent edges ────────────────────────────
        # These edges depend on profile statistics (top_values, distribution,
        # dtype, cardinality, etc.) and must be rebuilt when profiles change.

        # Remove old inferred edges (JOINABLE, DISTRIBUTION_SIMILAR,
        # SEMANTIC_SYNONYM, CORRELATED). Structural edges (CONTAINS,
        # FOREIGN_KEY) and semantic edges (REPRESENTS, HAS_CONCEPT) are kept.
        profile_dependent_types = [
            EdgeType.JOINABLE,
            EdgeType.DISTRIBUTION_SIMILAR,
            EdgeType.SEMANTIC_SYNONYM,
            EdgeType.CORRELATED,
        ]
        old_count = self.storage.remove_edges_by_types(profile_dependent_types)
        logger.info(f"Removed {old_count} old inferred edges for re-inference")

        # Re-compute inferred edges with updated profiles
        new_joinable = JoinableInferrer(overlap_threshold=self.config.joinable_overlap_threshold).infer(
            column_nodes, updated_profiles
        )

        new_synonym = SynonymInferrer().infer(column_nodes, updated_profiles)

        new_distribution = DistributionInferrer().infer(column_nodes, updated_profiles)

        new_correlated = CorrelationInferrer(correlation_threshold=self.config.correlation_threshold).infer(
            column_nodes, updated_profiles, new_joinable, all_ds_infos
        )

        new_inferred_edges = new_joinable + new_synonym + new_distribution + new_correlated
        logger.info(
            f"Re-inferred {len(new_inferred_edges)} edges "
            f"(joinable={len(new_joinable)}, synonym={len(new_synonym)}, "
            f"distribution={len(new_distribution)}, correlated={len(new_correlated)})"
        )

        # Store new inferred edges
        all_node_ids = set(self.storage.get_all_node_ids())
        self._store_edges(new_inferred_edges, all_node_ids)

        stats = self.storage.get_graph_stats()
        logger.info(f"Profile rebuild complete: {len(updated_profiles)} profiles, {len(new_inferred_edges)} edges")

        return {
            "status": "success",
            "profiles_updated": len(updated_profiles),
            "edges_removed": old_count,
            "edges_rebuilt": len(new_inferred_edges),
            "stats": stats,
        }

    # ── Embedding construction helpers ───────────────────────────────────

    def _build_embeddings_from_pipeline_result(self, result: dict[str, Any]) -> None:
        """Build embedding vectors for all nodes in a pipeline result.

        Called after _store_pipeline_result in init_build and rebuild_full.
        Only active when embedding service is configured and available.
        """
        if not self.embedding_service or not self.embedding_service.is_available():
            logger.debug("Embedding service not available — skipping vector construction")
            return

        # Collect nodes + profiles from pipeline result
        nodes_and_profiles = self._collect_nodes_with_profiles_from_result(result)
        if not nodes_and_profiles:
            return

        # Generate searchable text
        texts = []
        for node, profile in nodes_and_profiles:
            if isinstance(node, TableNode):
                col_names = [c.name for c in result["columns"] if c.table_id == node.id][:10]
                texts.append(node_to_searchable_text(node, profile, column_names=col_names))
            else:
                texts.append(node_to_searchable_text(node, profile))

        # Compute embeddings
        model_name = self.embedding_service.get_model_name()
        embeddings = self.embedding_service.compute_embeddings(texts)

        if not embeddings or len(embeddings) != len(texts):
            logger.warning("Embedding computation failed — vectors not built")
            return

        # Store embeddings
        data = [(node.id, model_name, embeddings[i], texts[i]) for i, (node, profile) in enumerate(nodes_and_profiles)]
        self.storage.add_embeddings_batch(data)
        self.storage.set_metadata("embedding_model", model_name)
        logger.info(f"Built {len(data)} embedding vectors with model {model_name}")

    def _build_embeddings_incremental(self) -> None:
        """Build embedding vectors for nodes that don't yet have embeddings.

        Called after add_datasource to embed only the new nodes.
        Only active when embedding service is configured and available.
        """
        if not self.embedding_service or not self.embedding_service.is_available():
            logger.debug("Embedding service not available — skipping incremental vector construction")
            return

        # Check if the stored embedding model matches current config
        stored_model = self.storage.get_embedding_model()
        current_model = self.embedding_service.get_model_name()

        if stored_model and stored_model != current_model:
            logger.warning(
                f"Stored embedding model '{stored_model}' differs from current '{current_model}'. "
                f"Run rebuild --mode vec to update all vectors."
            )
            return

        # Find nodes without embeddings
        all_embeddings = self.storage.get_all_embeddings()
        embedded_ids = {e["node_id"] for e in all_embeddings}

        nodes_and_profiles = self._collect_nodes_with_profiles()
        unembedded = [(n, p) for n, p in nodes_and_profiles if n.id not in embedded_ids]

        if not unembedded:
            return

        # Generate searchable text + compute embeddings
        texts = []
        for node, profile in unembedded:
            if isinstance(node, TableNode):
                col_names = self._get_column_names_for_table(node)
                texts.append(node_to_searchable_text(node, profile, column_names=col_names))
            else:
                texts.append(node_to_searchable_text(node, profile))

        embeddings = self.embedding_service.compute_embeddings(texts)

        if not embeddings or len(embeddings) != len(texts):
            logger.warning("Incremental embedding computation failed")
            return

        # Store embeddings
        data = [(node.id, current_model, embeddings[i], texts[i]) for i, (node, profile) in enumerate(unembedded)]
        self.storage.add_embeddings_batch(data)

        # Set metadata if this is the first batch of embeddings
        if not stored_model:
            self.storage.set_metadata("embedding_model", current_model)

        logger.info(f"Built {len(data)} incremental embedding vectors")

    def _collect_nodes_with_profiles(self) -> list[tuple[Node, ColumnProfile | None]]:
        """Collect all nodes from DB with their associated profiles (for Column nodes)."""
        result: list[tuple[Node, ColumnProfile | None]] = []

        for node_type in (NodeType.TABLE, NodeType.COLUMN, NodeType.CONCEPT, NodeType.ENTITY):
            nodes = self.storage.get_nodes_by_type(node_type)
            for node in nodes:
                profile = None
                if isinstance(node, ColumnNode):
                    profile = self.storage.get_profile_for_column(node.id)
                result.append((node, profile))

        return result

    def _collect_nodes_with_profiles_from_result(
        self, result: dict[str, Any]
    ) -> list[tuple[Node, ColumnProfile | None]]:
        """Collect nodes with profiles from a pipeline result dict."""
        profile_map = {p.column_id: p for p in result["profiles"]}

        items: list[tuple[Node, ColumnProfile | None]] = []

        for table in result["tables"]:
            items.append((table, None))
        for col in result["columns"]:
            items.append((col, profile_map.get(col.id)))
        for concept in result["concepts"]:
            items.append((concept, None))
        for entity in result["entities"]:
            items.append((entity, None))

        return items

    def _get_column_names_for_table(self, table: TableNode) -> list[str]:
        """Get column names for a table from stored data."""
        col_names = []
        for cid in table.column_ids[:10]:
            col = self.storage.get_node(cid)
            if col:
                col_names.append(col.name)
        return col_names

    # ── Helpers ──────────────────────────────────────────────────────────

    def _update_column_properties(self, columns: list[ColumnNode], profiles: list[ColumnProfile]) -> None:
        """Update column nodes with profile references and semantic types."""
        profile_map = {p.column_id: p for p in profiles}
        for col in columns:
            profile = profile_map.get(col.id)
            if profile:
                col.semantic_type = profile.semantic_type
                col.profile_id = profile.id
                col.properties["dtype"] = col.dtype
                col.properties["semantic_type"] = col.semantic_type
                col.properties["table_id"] = col.table_id
                col.properties["profile_id"] = col.profile_id
                col.properties["comment"] = col.comment

    def _update_table_properties(self, tables: list[TableNode]) -> None:
        """Update table nodes with properties for DB storage."""
        for table in tables:
            table.properties["source"] = table.source
            table.properties["source_type"] = table.source_type
            table.properties["row_count"] = table.row_count
            table.properties["column_ids"] = table.column_ids

    def _store_edges(self, edges: list[Edge], available_node_ids: set[str]) -> tuple[int, int]:
        """Store edges, splitting into resolved and pending.

        Returns (resolved_count, pending_count).
        """
        resolved: list[Edge] = []
        pending: list[PendingEdge] = []

        for edge in edges:
            if edge.confidence < self.config.confidence_threshold:
                continue
            src_exists = edge.source_id in available_node_ids
            tgt_exists = edge.target_id in available_node_ids
            if src_exists and tgt_exists:
                resolved.append(edge)
            else:
                missing = []
                if not src_exists:
                    missing.append("source")
                if not tgt_exists:
                    missing.append("target")
                pending.append(
                    PendingEdge(
                        id=edge.id,
                        source_id=edge.source_id,
                        target_id=edge.target_id,
                        type=edge.type,
                        confidence=edge.confidence,
                        properties=edge.properties,
                        missing_endpoints=missing,
                    )
                )

        self.storage.add_edges_batch(resolved)
        self.storage.add_pending_edges_batch(pending)
        return len(resolved), len(pending)
