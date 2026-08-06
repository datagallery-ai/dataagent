"""LLMMapper — map structural nodes to semantic layer using LLM inference.

All columns are mapped via LLM (columns with comments include them as
extra signal in the prompt). When adding new data to an existing graph
(add_table), new Concepts and Entities are merged with existing ones
via a two-stage approach:
  1. Embedding pre-filter (optional): compute cosine similarity between
     new/existing nodes to narrow down candidate merge pairs.
  2. LLM merge judgment (required): send candidates + full node lists
     to LLM for semantic confirmation of merge decisions.

Also generates table comments (descriptions) for tables that lack
SQL metadata comments, using column names, profiles, and inferred
concept/entity info as input.
"""

import json
import logging
import math
from pathlib import Path
from typing import Any

from datalink.config import DataLinkConfig
from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, Node, TableNode
from datalink.models.profile import ColumnProfile

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Prompt templates — loaded from external files so they can be
# edited/tuned without touching Python code.
MAPPING_PROMPT_TEMPLATE = (_PROMPTS_DIR / "mapping_prompt.txt").read_text(encoding="utf-8")
TABLE_COMMENT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "table_comment_prompt.txt").read_text(encoding="utf-8")
MERGE_PROMPT_TEMPLATE = (_PROMPTS_DIR / "merge_prompt.txt").read_text(encoding="utf-8")


class LLMMapper:
    """Map structural nodes to Concept/Entity using LLM inference.

    All columns (including those with comments) are mapped via LLM.
    Columns with comments include them as extra signal in the prompt.

    When adding new data to an existing graph (add_table), new Concepts
    and Entities are merged with existing ones via:
    - Embedding pre-filter (if configured) for candidate pair narrowing
    - LLM judgment for semantic merge confirmation
    - Edge redirection and deduplication
    """

    # Batch size for LLM calls — smaller batches reduce JSON output
    # complexity and improve success rate for weaker models.
    # Can be overridden via config.mapping_batch_size.
    _DEFAULT_BATCH_SIZE = 15

    def __init__(self, config: DataLinkConfig):
        """Initialize with full DataLink configuration.

        Args:
            config: DataLink configuration including LLM and embedding settings.
        """
        self.config = config
        self.llm_config = config.llm
        self.embedding_config = config.embedding
        self._batch_size = config.mapping_batch_size or self._DEFAULT_BATCH_SIZE
        self._client = None

    def map_columns(
        self,
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
    ) -> tuple[list[ConceptNode], list[EntityNode], list[Edge]]:
        """Map columns to semantic Concept/Entity nodes using LLM inference.

        Columns are processed in batches to reduce JSON output complexity,
        which significantly improves success rate for weaker LLM models.
        Each batch is independently parsed — if one batch fails, the others
        still contribute their results.

        Between batches, results are merged using the same merge_with_existing
        logic to deduplicate concepts/entities that different batches may
        have named differently (e.g., batch 1 says "person_id", batch 2
        says "person_identifier").

        Args:
            columns: All column nodes to map.
            profiles: Corresponding column profiles.

        Returns:
            Tuple of (concept_nodes, entity_nodes, represents_edges + has_concept_edges).
        """
        if not columns:
            return [], [], []

        profile_map = {p.column_id: p for p in profiles}

        # Small column counts — single call is fine
        if len(columns) <= self._batch_size:
            return self._map_columns_single(columns, profile_map)

        # Larger sets — batch to reduce JSON complexity, accumulate
        # multiple batches before merging to reduce LLM merge call frequency.
        all_concepts: list[ConceptNode] = []
        all_entities: list[EntityNode] = []
        all_edges: list[Edge] = []

        # Unmerged batch accumulations — collected between merge intervals
        pending_concepts: list[ConceptNode] = []
        pending_entities: list[EntityNode] = []
        pending_edges: list[Edge] = []

        total_batches = (len(columns) + self._batch_size - 1) // self._batch_size
        success_batches = 0
        merge_interval = self.config.merge_batch_interval

        for i in range(total_batches):
            start = i * self._batch_size
            end = min(start + self._batch_size, len(columns))
            batch = columns[start:end]

            logger.info(f"Mapping batch {i + 1}/{total_batches} ({len(batch)} columns)")
            batch_concepts, batch_entities, batch_edges = self._map_columns_single(batch, profile_map)

            if batch_concepts or batch_entities:
                success_batches += 1
                # Accumulate into pending pile (no merge yet)
                pending_concepts.extend(batch_concepts)
                pending_entities.extend(batch_entities)
                pending_edges.extend(batch_edges)
            else:
                logger.warning(f"Batch {i + 1}/{total_batches} failed — skipping {len(batch)} columns")

            # Merge every N batches, or on the last batch.
            # The first merge point establishes the "accumulated" base.
            # Subsequent merge points merge pending into accumulated.
            is_merge_point = (i + 1) % merge_interval == 0 or (i + 1) == total_batches

            if is_merge_point and (pending_concepts or pending_entities):
                if not all_concepts and not all_entities:
                    # First merge point: no accumulated base yet.
                    # merge_with_existing handles the "no existing nodes" case
                    # by doing a self-merge to deduplicate within the group.
                    logger.info(
                        f"First merge point: merging {len(pending_concepts)} concepts, "
                        f"{len(pending_entities)} entities from batches 1-{i + 1}"
                    )
                    all_concepts, all_entities, all_edges = self.merge_with_existing(
                        pending_concepts,
                        pending_entities,
                        pending_edges,
                        [],
                        [],
                    )
                    pending_concepts = []
                    pending_entities = []
                    pending_edges = []
                else:
                    # Subsequent merge point: merge pending into accumulated.
                    logger.info(
                        f"Merging accumulated {len(pending_concepts)} concepts, "
                        f"{len(pending_entities)} entities from recent batches "
                        f"into {len(all_concepts)} existing concepts, {len(all_entities)} entities"
                    )
                    all_concepts, all_entities, all_edges = self.merge_with_existing(
                        pending_concepts,
                        pending_entities,
                        pending_edges,
                        all_concepts,
                        all_entities,
                        all_edges,
                    )
                    pending_concepts = []
                    pending_entities = []
                    pending_edges = []

        # If there are still pending nodes that haven't been merged
        # (e.g., all batches failed or were empty), just append them.
        if pending_concepts or pending_entities:
            if all_concepts or all_entities:
                all_concepts, all_entities, all_edges = self.merge_with_existing(
                    pending_concepts,
                    pending_entities,
                    pending_edges,
                    all_concepts,
                    all_entities,
                    all_edges,
                )
            else:
                # No accumulated results yet — just use pending as the base
                all_concepts = pending_concepts
                all_entities = pending_entities
                all_edges = pending_edges

        logger.info(
            f"LLMMapper: {success_batches}/{total_batches} batches succeeded, "
            f"created {len(all_concepts)} concepts, {len(all_entities)} entities, "
            f"{len(all_edges)} edges for {len(columns)} columns"
        )
        return all_concepts, all_entities, all_edges

    def _map_columns_single(
        self,
        columns: list[ColumnNode],
        profile_map: dict[str, ColumnProfile],
    ) -> tuple[list[ConceptNode], list[EntityNode], list[Edge]]:
        """Map a single batch of columns via one LLM call."""
        columns_data = self._build_columns_data(columns, profile_map)

        # Call LLM
        prompt = MAPPING_PROMPT_TEMPLATE.format(columns_data=columns_data)
        response = self._call_llm(prompt)

        if not response:
            logger.warning("LLM returned no response, skipping semantic mapping")
            return [], [], []

        # Parse response
        parsed = self._parse_response(response)

        if not parsed:
            logger.warning("Failed to parse LLM response, skipping semantic mapping")
            return [], [], []

        # Convert to nodes and edges
        return self._build_nodes_and_edges(parsed, columns, profile_map)

    def _build_columns_data(self, columns: list[ColumnNode], profile_map: dict[str, ColumnProfile]) -> str:
        """Build the column data section for the LLM prompt."""
        lines = []
        for col in columns:
            profile = profile_map.get(col.id)
            if profile is None:
                continue

            data = {
                "column_id": col.id,
                "column_name": col.name,
                "table_id": col.table_id,
                "dtype": profile.dtype,
                "semantic_type": profile.semantic_type,
                "null_rate": profile.null_rate,
                "cardinality": profile.cardinality,
                "unique_rate": profile.unique_rate,
            }

            # Add numeric stats
            if profile.min_value is not None:
                data["min_value"] = profile.min_value
            if profile.max_value is not None:
                data["max_value"] = profile.max_value
            if profile.mean_value is not None:
                data["mean_value"] = round(profile.mean_value, 2)

            # Add sample values
            if profile.sample_values:
                data["sample_values"] = [str(v) for v in profile.sample_values[:5]]

            # Add comment if available
            if col.comment:
                data["comment"] = col.comment

            lines.append(json.dumps(data))

        return "\n".join(lines)

    def _call_llm(self, prompt: str, temperature: float | None = None) -> str | None:
        """Call the LLM via OpenAI-compatible Chat Completions API.

        Uses the OpenAI SDK with configurable base_url, supporting any
        service that implements the OpenAI Chat Completions protocol
        (OpenAI, Azure, vLLM, Ollama, DeepSeek, etc.).

        Args:
            prompt: The user prompt to send.
            temperature: Override temperature for this call. None = use self.llm_config.temperature.
        """
        api_key = self.llm_config.get_api_key()
        if not api_key:
            logger.warning("No API key configured for LLM, skipping call")
            return None

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=self.llm_config.base_url,
                timeout=self.llm_config.timeout,
            )

            temp = temperature if temperature is not None else self.llm_config.temperature

            response = client.chat.completions.create(
                model=self.llm_config.model,
                messages=[
                    {"role": "system", "content": "You are a data semantic analyzer. Always respond with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=temp,
                max_tokens=self.llm_config.max_tokens,
                response_format={"type": "json_object"},
            )

            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return None

    def _parse_response(self, response: str) -> dict[str, Any] | None:
        """Parse the LLM response into a structured dict.

        The response should be JSON, but LLMs sometimes add extra text
        before/after the JSON block or produce slightly invalid JSON.
        Uses progressive repair attempts before giving up.
        """
        parsed = self._try_parse_json(response)

        if parsed is None:
            logger.warning(f"Failed to parse LLM response after all repair attempts: {response[:200]}")
            return None

        # Validate structure
        if "concepts" not in parsed or "entities" not in parsed:
            logger.warning("LLM response missing 'concepts' or 'entities' keys")
            return None

        return parsed

    def _try_parse_json(self, text: str) -> dict[str, Any] | None:
        """Progressively attempt to parse JSON from LLM output.

        Weak models often produce slightly invalid JSON. This method
        tries progressively more aggressive repairs:

        1. Direct json.loads
        2. Extract markdown code block (```json ... ```)
        3. Remove inline comments (// and /* */)
        4. Remove trailing commas before } and ]
        5. Fix unclosed brackets by appending missing } and ]
        """
        import re

        # Step 1: Direct parse
        result = self._extract_and_parse(text)
        if result is not None:
            return result

        # Step 2: Extract markdown code block
        code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_block_match:
            result = self._extract_and_parse(code_block_match.group(1))
            if result is not None:
                logger.debug("Parsed JSON from markdown code block")
                return result

        # Steps 3-5 build on each other, applying repairs cumulatively
        working = text

        # Step 3: Remove comments
        working = re.sub(r"/\*.*?\*/", "", working, flags=re.DOTALL)
        working = re.sub(r",\s*//[^\n]*?(?=\s*})", "", working)
        working = re.sub(r",\s*//[^\n]*?(?=\s*\])", "", working)
        working = re.sub(r"\s*//[^\n]*?(?=\s*})", "", working)
        working = re.sub(r"\s*//[^\n]*?(?=\s*\])", "", working)
        working = re.sub(r"\s*//.*$", "", working, flags=re.MULTILINE)

        result = self._extract_and_parse(working)
        if result is not None:
            logger.debug("Parsed JSON after removing comments")
            return result

        # Step 4: Remove trailing commas (after comment removal)
        working = re.sub(r",\s*}", "}", working)
        working = re.sub(r",\s*\]", "]", working)

        result = self._extract_and_parse(working)
        if result is not None:
            logger.debug("Parsed JSON after removing trailing commas")
            return result

        # Step 5: Fix unclosed brackets
        bracket_fixed = self._fix_brackets(working)

        result = self._extract_and_parse(bracket_fixed)
        if result is not None:
            logger.debug("Parsed JSON after fixing brackets")
            return result

        return None

    def _extract_and_parse(self, text: str) -> dict[str, Any] | None:
        """Extract the outermost JSON object from text and parse it."""
        json_start = text.find("{")
        json_end = text.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            return None

        json_str = text[json_start:json_end]

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            return None

    def _fix_brackets(self, text: str) -> str:
        """Attempt to fix unclosed brackets by appending missing } and ]."""
        json_start = text.find("{")
        if json_start == -1:
            return text

        json_str = text[json_start:]

        # Count unclosed brackets
        open_curly = 0
        open_square = 0
        in_string = False
        escape_next = False

        for char in json_str:
            if escape_next:
                escape_next = False
                continue
            if char == "\\":
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                open_curly += 1
            elif char == "}":
                open_curly -= 1
            elif char == "[":
                open_square += 1
            elif char == "]":
                open_square -= 1

        # Append missing closing brackets
        suffix = "]" * max(0, open_square) + "}" * max(0, open_curly)
        if suffix:
            return json_str + suffix
        return text

    def _build_nodes_and_edges(
        self,
        parsed: dict[str, Any],
        columns: list[ColumnNode],
        profile_map: dict[str, ColumnProfile],
    ) -> tuple[list[ConceptNode], list[EntityNode], list[Edge]]:
        """Convert parsed LLM response into Concept/Entity nodes and edges."""
        concepts: list[ConceptNode] = []
        entities: list[EntityNode] = []
        edges: list[Edge] = []

        # Build Concept nodes
        for concept_data in parsed.get("concepts", []):
            concept = ConceptNode(
                id=f"concept:{concept_data['name']}",
                name=concept_data["name"],
                description=concept_data.get("description") or "",
                unit=concept_data.get("unit") or "",
                dimension=concept_data.get("dimension") or "",
                properties={
                    "source": "llm_inference",
                    "confidence": concept_data.get("confidence") or 0.7,
                },
            )
            concepts.append(concept)

            # Create represents edges: Column → Concept
            for col_id in concept_data.get("columns", []):
                edge = Edge(
                    id=f"edge:represents:{col_id}:{concept.id}",
                    source_id=col_id,
                    target_id=concept.id,
                    type=EdgeType.REPRESENTS,
                    confidence=concept_data.get("confidence", 0.7),
                    properties={
                        "source": "llm_inference",
                    },
                )
                edges.append(edge)

        # Build Entity nodes
        for entity_data in parsed.get("entities", []):
            entity = EntityNode(
                id=f"entity:{entity_data['name']}",
                name=entity_data["name"],
                description=entity_data.get("description") or "",
                properties={
                    "source": "llm_inference",
                    "confidence": entity_data.get("confidence") or 0.7,
                },
            )
            entities.append(entity)

            # Create has_concept edges: Entity → Concept
            for concept_name in entity_data.get("concept_names", []):
                concept_id = f"concept:{concept_name}"
                edge = Edge(
                    id=f"edge:has_concept:{entity.id}:{concept_id}",
                    source_id=entity.id,
                    target_id=concept_id,
                    type=EdgeType.HAS_CONCEPT,
                    confidence=entity_data.get("confidence", 0.7),
                    properties={
                        "source": "llm_inference",
                    },
                )
                edges.append(edge)

        return concepts, entities, edges

    # --- Concept/Entity merge with existing graph (LLM + Embedding) ---

    def merge_with_existing(
        self,
        new_concepts: list[ConceptNode],
        new_entities: list[EntityNode],
        new_edges: list[Edge],
        existing_concepts: list[ConceptNode],
        existing_entities: list[EntityNode],
        existing_edges: list[Edge] | None = None,
    ) -> tuple[list[ConceptNode], list[EntityNode], list[Edge]]:
        """Merge new Concepts/Entities with existing ones in the graph.

        Two-stage approach:
        1. Embedding pre-filter (optional): compute cosine similarity
           between new/existing nodes to identify candidate merge pairs.
        2. LLM merge judgment (required): send candidates + node lists
           to LLM for semantic confirmation.

        After LLM returns a merge plan:
        - Merged nodes are absorbed (description enriched, edges redirected)
        - Genuinely new nodes are kept
        - Both existing and new edges are redirected if they reference
          merged-out nodes, then deduplicated by (source, target, type)
          keeping highest confidence

        Args:
            new_concepts: Concepts from the latest LLM call.
            new_entities: Entities from the latest LLM call.
            new_edges: Edges from the latest LLM call.
            existing_concepts: Concepts already in the graph.
            existing_entities: Entities already in the graph.
            existing_edges: Edges already accumulated from prior merges.
                These are redirected and included alongside new edges.

        Returns:
            Tuple of (final_concepts, final_entities, final_edges) after
            merging and deduplication.
        """
        if not existing_concepts and not existing_entities:
            # No existing nodes — but we still need to deduplicate within
            # the new nodes themselves (e.g., different batches may produce
            # concept:person_id and concept:person_identifier for the same thing).
            # Pass all new nodes as both new and existing to force a self-merge,
            # but use a clean split to avoid duplicate entries in the result.
            all_edges_in = list(existing_edges or []) + list(new_edges)
            if len(new_concepts) > 1 or len(new_entities) > 1:
                logger.info(
                    f"Self-merging {len(new_concepts)} concepts, {len(new_entities)} entities (no existing nodes)"
                )
                # Split: first node becomes "existing base", rest are "new"
                # This forces LLM to evaluate whether any "new" nodes duplicate
                # the first, and whether any "new" nodes duplicate each other
                # (LLM can compare across all items in the prompt).
                base_c = new_concepts[:1]
                base_e = new_entities[:1] if new_entities else []
                rest_c = new_concepts[1:]
                rest_e = new_entities[1:] if new_entities else []

                result_c, result_e, result_edges = self.merge_with_existing(
                    rest_c,
                    rest_e,
                    all_edges_in,
                    base_c,
                    base_e,
                )
                return result_c, result_e, result_edges
            else:
                # Only 1 concept/entity — no duplicates possible
                return new_concepts, new_entities, all_edges_in

        if not new_concepts and not new_entities:
            # No new nodes to merge — return existing + existing_edges
            all_edges_in = list(existing_edges or []) + list(new_edges)
            return existing_concepts, existing_entities, all_edges_in

        # Step 1: Embedding pre-filter (optional)
        candidates = self._embedding_prefilter(
            new_concepts,
            new_entities,
            existing_concepts,
            existing_entities,
        )

        # Step 2: LLM merge judgment
        merge_plan = self._llm_merge_judge(
            new_concepts,
            new_entities,
            existing_concepts,
            existing_entities,
            candidates,
        )

        if merge_plan is None:
            # LLM call failed — skip merge, keep all new nodes as-is,
            # and preserve existing edges alongside new edges.
            logger.warning("LLM merge judgment failed — keeping all new nodes without merging")
            combined_edges = list(existing_edges or []) + list(new_edges)
            return new_concepts, new_entities, combined_edges

        # Step 3: Execute merge plan
        final_concepts, final_entities, final_edges = self._execute_merge_plan(
            merge_plan,
            new_concepts,
            new_entities,
            new_edges,
            existing_concepts,
            existing_entities,
            existing_edges=existing_edges,
        )

        # Step 4: Deduplicate edges by (source_id, target_id, type)
        deduped_edges = self._deduplicate_edges(final_edges)

        merged_count = len(merge_plan.get("merges", []))
        if merged_count:
            new_concept_count = len(final_concepts) - len(existing_concepts)
            new_entity_count = len(final_entities) - len(existing_entities)
            logger.info(
                f"Concept/Entity merge: {merged_count} merges, "
                f"{new_concept_count} new concepts kept (total {len(final_concepts)}), "
                f"{new_entity_count} new entities kept (total {len(final_entities)}), "
                f"{len(deduped_edges)} edges after dedup"
            )

        return final_concepts, final_entities, deduped_edges

    def _embedding_prefilter(
        self,
        new_concepts: list[ConceptNode],
        new_entities: list[EntityNode],
        existing_concepts: list[ConceptNode],
        existing_entities: list[EntityNode],
    ) -> list[dict[str, Any]]:
        """Pre-filter candidate merge pairs using embedding similarity.

        Returns a list of candidate pairs: {"new_id", "existing_id",
        "new_name", "existing_name", "similarity"}.

        If embedding is not configured (model is empty) or the API call
        fails, returns an empty list and the LLM judge will work on the
        full set without pre-filtering.
        """
        if not self.embedding_config.model:
            logger.debug("Embedding model not configured — skipping pre-filter")
            return []

        api_key = self.embedding_config.get_api_key(self.llm_config)
        if not api_key:
            logger.debug("No embedding API key — skipping pre-filter")
            return []

        try:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                base_url=self.embedding_config.get_base_url(self.llm_config),
                timeout=self.embedding_config.timeout,
            )

            # Build text representations for all nodes
            new_nodes = new_concepts + new_entities
            existing_nodes = existing_concepts + existing_entities

            new_texts = [self._node_to_embedding_text(n) for n in new_nodes]
            existing_texts = [self._node_to_embedding_text(n) for n in existing_nodes]

            if not new_texts or not existing_texts:
                return []

            # Compute embeddings — batch call
            new_response = client.embeddings.create(
                model=self.embedding_config.model,
                input=new_texts,
            )
            existing_response = client.embeddings.create(
                model=self.embedding_config.model,
                input=existing_texts,
            )

            new_embeddings = [item.embedding for item in new_response.data]
            existing_embeddings = [item.embedding for item in existing_response.data]

            # Compute cosine similarity matrix
            candidates = []
            threshold = self.embedding_config.similarity_threshold

            for i, new_node in enumerate(new_nodes):
                for j, existing_node in enumerate(existing_nodes):
                    sim = self._cosine_similarity(new_embeddings[i], existing_embeddings[j])
                    if sim >= threshold:
                        candidates.append(
                            {
                                "new_id": new_node.id,
                                "existing_id": existing_node.id,
                                "new_name": new_node.name,
                                "existing_name": existing_node.name,
                                "similarity": round(sim, 4),
                            }
                        )

            logger.info(
                f"Embedding pre-filter: {len(candidates)} candidate pairs "
                f"(threshold={threshold}, {len(new_nodes)} new vs {len(existing_nodes)} existing)"
            )
            return candidates

        except Exception as e:
            logger.warning(f"Embedding pre-filter failed: {e} — falling back to full LLM judgment")
            return []

    def _node_to_embedding_text(self, node: Node) -> str:
        """Convert a Concept/Entity node to text for embedding computation.

        Combines name, description, and type-specific fields to produce
        a rich text representation that captures semantic meaning.
        """
        parts = [node.name]

        if isinstance(node, ConceptNode):
            if node.description:
                parts.append(node.description)
            if node.unit:
                parts.append(f"unit: {node.unit}")
            if node.dimension:
                parts.append(f"dimension: {node.dimension}")
        elif isinstance(node, EntityNode):
            if node.description:
                parts.append(node.description)

        return " | ".join(parts)

    def _cosine_similarity(self, vec_a: list[float], vec_b: list[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))

        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0

        return dot / (norm_a * norm_b)

    def _llm_merge_judge(
        self,
        new_concepts: list[ConceptNode],
        new_entities: list[EntityNode],
        existing_concepts: list[ConceptNode],
        existing_entities: list[EntityNode],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Send new/existing nodes and candidate pairs to LLM for merge judgment.

        When embedding pre-filter produced candidates, only inject existing
        nodes that appear in those candidate pairs — nodes below the similarity
        threshold are excluded entirely since embedding already considered
        name + description + unit + dimension, making them extremely unlikely
        to need merging. This dramatically reduces prompt size for large graphs.

        Returns the merge plan dict:
        {"merges": [{"new_id", "existing_id", "reason", "confidence"}],
         "new_kept": [id, ...]}

        If the LLM call fails, returns None.
        """
        # Determine which existing nodes are relevant for merging.
        # When candidates exist (embedding pre-filter succeeded), only
        # include existing nodes that appear in candidate pairs — nodes
        # below the similarity threshold are excluded entirely.
        if candidates:
            related_existing_ids = {c["existing_id"] for c in candidates}
            filtered_existing_concepts = [c for c in existing_concepts if c.id in related_existing_ids]
            filtered_existing_entities = [e for e in existing_entities if e.id in related_existing_ids]
            existing_note = (
                f"Only {len(filtered_existing_concepts)} concepts and "
                f"{len(filtered_existing_entities)} entities are shown out of "
                f"{len(existing_concepts)} total concepts and {len(existing_entities)} total entities "
                f"— the rest were filtered out by embedding similarity as too dissimilar to any new node."
            )
        else:
            # No embedding pre-filter — include all existing nodes so LLM
            # can evaluate every pair (fallback for correctness).
            filtered_existing_concepts = existing_concepts
            filtered_existing_entities = existing_entities
            existing_note = "All existing nodes are shown (no embedding pre-filter was applied)."

        # Build data for prompt
        new_concepts_data = self._build_merge_node_data(new_concepts, "concept")
        new_entities_data = self._build_merge_node_data(new_entities, "entity")
        existing_concepts_data = self._build_merge_node_data(filtered_existing_concepts, "concept")
        existing_entities_data = self._build_merge_node_data(filtered_existing_entities, "entity")

        candidates_data = ""
        if candidates:
            candidates_lines = [json.dumps(c) for c in candidates]
            candidates_data = "\n".join(candidates_lines)
        else:
            candidates_data = "No pre-filter candidates — evaluate all pairs directly."

        prompt = MERGE_PROMPT_TEMPLATE.format(
            new_concepts_data=new_concepts_data,
            new_entities_data=new_entities_data,
            existing_concepts_data=existing_concepts_data,
            existing_entities_data=existing_entities_data,
            candidates_data=candidates_data,
            existing_note=existing_note,
        )

        response = self._call_llm(prompt, temperature=self.config.merge_llm_temperature)

        if not response:
            return None

        # Parse the merge response
        parsed = self._try_parse_json(response)

        if parsed is None:
            logger.warning(f"Failed to parse merge response: {response[:200]}")
            return None

        # Validate structure
        if "merges" not in parsed:
            logger.warning("Merge response missing 'merges' key")
            return None

        # Filter merges by confidence threshold (from config)
        min_confidence = self.config.confidence_threshold
        parsed["merges"] = [m for m in parsed["merges"] if m.get("confidence", 0) >= min_confidence]

        # Ensure new_kept exists
        if "new_kept" not in parsed:
            parsed["new_kept"] = []

        return parsed

    def _build_merge_node_data(self, nodes: list[Node], kind: str) -> str:
        """Build the node data section for the merge prompt.

        Args:
            nodes: List of Concept or Entity nodes.
            kind: "concept" or "entity" — used for labeling.
        """
        if not nodes:
            return f"No {kind}s."

        lines = []
        for node in nodes:
            data = {"id": node.id, "name": node.name}

            if isinstance(node, ConceptNode):
                data["description"] = node.description
                data["unit"] = node.unit
                data["dimension"] = node.dimension
            elif isinstance(node, EntityNode):
                data["description"] = node.description

            lines.append(json.dumps(data))

        return "\n".join(lines)

    def _execute_merge_plan(
        self,
        merge_plan: dict[str, Any],
        new_concepts: list[ConceptNode],
        new_entities: list[EntityNode],
        new_edges: list[Edge],
        existing_concepts: list[ConceptNode],
        existing_entities: list[EntityNode],
        existing_edges: list[Edge] | None = None,
    ) -> tuple[list[ConceptNode], list[EntityNode], list[Edge]]:
        """Execute the merge plan returned by LLM.

        For each merge decision:
        - The new node is absorbed into the existing node
        - Description is enriched if the new one has more info
        - Properties from the new node are merged into the existing one
        - All edges (both existing and new) referencing merged-out nodes
          are redirected

        Nodes listed in new_kept are preserved as genuinely new nodes.

        Args:
            merge_plan: LLM merge response with "merges" and "new_kept" lists.
            new_concepts: New concepts to process.
            new_entities: New entities to process.
            new_edges: New edges to process.
            existing_concepts: Existing concepts (may be enriched).
            existing_entities: Existing entities (may be enriched).
            existing_edges: Edges accumulated from prior merges; these
                are also redirected if they reference merged-out nodes.

        Returns:
            Tuple of (kept_new_concepts, kept_new_entities, final_edges)
            where final_edges includes both redirected existing edges and
            redirected new edges.
        """
        # Build merge maps from the plan
        concept_merge_map: dict[str, str] = {}  # new_concept_id → existing_concept_id
        entity_merge_map: dict[str, str] = {}  # new_entity_id → existing_entity_id

        for merge in merge_plan.get("merges", []):
            new_id = merge.get("new_id", "")
            existing_id = merge.get("existing_id", "")
            if not new_id or not existing_id:
                continue

            if new_id.startswith("concept:"):
                concept_merge_map[new_id] = existing_id
            elif new_id.startswith("entity:"):
                entity_merge_map[new_id] = existing_id

        # Build lookups for existing nodes (for enrichment)
        existing_concept_map = {c.id: c for c in existing_concepts}
        existing_entity_map = {e.id: e for e in existing_entities}

        # Build lookups for new nodes (for enrichment and filtering)
        new_concept_map = {c.id: c for c in new_concepts}
        new_entity_map = {e.id: e for e in new_entities}

        # Enrich existing nodes with merged descriptions and properties
        for new_id, existing_id in concept_merge_map.items():
            new_c = new_concept_map.get(new_id)
            existing_c = existing_concept_map.get(existing_id)
            if new_c and existing_c:
                if len(new_c.description) > len(existing_c.description):
                    existing_c.description = new_c.description
                for key, value in new_c.properties.items():
                    if key not in existing_c.properties:
                        existing_c.properties[key] = value
                logger.debug(f"Merged concept '{new_c.name}' into existing '{existing_c.name}'")

        for new_id, existing_id in entity_merge_map.items():
            new_e = new_entity_map.get(new_id)
            existing_e = existing_entity_map.get(existing_id)
            if new_e and existing_e:
                if len(new_e.description) > len(existing_e.description):
                    existing_e.description = new_e.description
                for key, value in new_e.properties.items():
                    if key not in existing_e.properties:
                        existing_e.properties[key] = value
                logger.debug(f"Merged entity '{new_e.name}' into existing '{existing_e.name}'")

        # Kept new nodes (not merged) are tracked below via final_concepts

        final_concepts: list[ConceptNode] = list(existing_concepts)  # Start with existing (enriched)
        for new_c in new_concepts:
            if new_c.id not in concept_merge_map:
                final_concepts.append(new_c)

        final_entities: list[EntityNode] = list(existing_entities)  # Start with existing (enriched)
        for new_e in new_entities:
            if new_e.id not in entity_merge_map:
                final_entities.append(new_e)

        # Redirect edges that reference merged-out nodes
        # Process both existing (accumulated) edges and new edges.
        final_edges: list[Edge] = []

        def _redirect_edge(edge: Edge) -> Edge:
            """Redirect an edge if it references a merged-out node."""
            if edge.type == EdgeType.REPRESENTS:
                # Column → Concept: only target_id may need redirecting
                redirected_target = concept_merge_map.get(edge.target_id, edge.target_id)
                if redirected_target != edge.target_id:
                    return Edge(
                        id=f"edge:represents:{edge.source_id}:{redirected_target}",
                        source_id=edge.source_id,
                        target_id=redirected_target,
                        type=edge.type,
                        confidence=edge.confidence,
                        properties=edge.properties,
                    )
            elif edge.type == EdgeType.HAS_CONCEPT:
                # Entity → Concept: both may need redirecting
                redirected_source = entity_merge_map.get(edge.source_id, edge.source_id)
                redirected_target = concept_merge_map.get(edge.target_id, edge.target_id)
                if redirected_source != edge.source_id or redirected_target != edge.target_id:
                    return Edge(
                        id=f"edge:has_concept:{redirected_source}:{redirected_target}",
                        source_id=redirected_source,
                        target_id=redirected_target,
                        type=edge.type,
                        confidence=edge.confidence,
                        properties=edge.properties,
                    )
            return edge

        # First: redirect existing (accumulated) edges
        for edge in existing_edges or []:
            final_edges.append(_redirect_edge(edge))

        # Then: redirect new edges
        for edge in new_edges:
            final_edges.append(_redirect_edge(edge))

        return final_concepts, final_entities, final_edges

    def _deduplicate_edges(self, edges: list[Edge]) -> list[Edge]:
        """Remove duplicate edges, keeping the one with highest confidence.

        Two edges are duplicates if they have the same (source_id, target_id, type).
        Among duplicates, keep the edge with the highest confidence score.

        Args:
            edges: List of edges (may contain duplicates after merge redirection).

        Returns:
            Deduplicated list of edges.
        """
        # Group by (source_id, target_id, type)
        groups: dict[tuple[str, str, str], list[Edge]] = {}
        for edge in edges:
            key = (edge.source_id, edge.target_id, edge.type.value)
            if key not in groups:
                groups[key] = []
            groups[key].append(edge)

        # For each group, keep the edge with highest confidence
        result = []
        for key, group_edges in groups.items():
            best = max(group_edges, key=lambda e: e.confidence)
            result.append(best)

        return result

    # ── Table comment generation ──────────────────────────────────────────

    def generate_table_comments(
        self,
        tables: list[TableNode],
        columns: list[ColumnNode],
        profiles: list[ColumnProfile],
        concepts: list[ConceptNode] | None = None,
        entities: list[EntityNode] | None = None,
        semantic_edges: list[Edge] | None = None,
    ) -> dict[str, str]:
        """Generate LLM-inferred comments for tables that lack SQL metadata comments.

        When a table has no comment from its source (SQL metadata, CSV header),
        this method uses LLM to infer a one-line description based on:
        - Table name
        - Column names, types, semantic_types
        - Column profiles (null_rate, cardinality, sample values)
        - Inferred concept/entity names (if available from Step 5)

        Args:
            tables: Table nodes to generate comments for.
            columns: All column nodes (will be grouped by table).
            profiles: Corresponding column profiles.
            concepts: Inferred concept nodes (optional, from Step 5).
            entities: Inferred entity nodes (optional, from Step 5).
            semantic_edges: Represents/has_concept edges (optional, from Step 5).

        Returns:
            Dict mapping table_id → inferred_comment string.
            Only includes tables that had no existing comment.
        """
        # Find tables without comments
        tables_without_comment = []
        for table in tables:
            existing_comment = table.properties.get("comment", "") or getattr(table, "comment", "")
            if not existing_comment.strip():
                tables_without_comment.append(table)

        if not tables_without_comment:
            logger.info("All tables have existing comments — skipping LLM comment generation")
            return {}

        logger.info(f"Generating comments for {len(tables_without_comment)} tables without SQL metadata")

        # Build column lookup: table_id → list of (ColumnNode, ColumnProfile)
        profile_map = {p.column_id: p for p in profiles}
        column_by_table: dict[str, list[tuple[ColumnNode, ColumnProfile | None]]] = {}
        for col in columns:
            tid = col.table_id
            if tid not in column_by_table:
                column_by_table[tid] = []
            column_by_table[tid].append((col, profile_map.get(col.id)))

        # Build concept/entity summary per table (from semantic edges)
        concept_by_column: dict[str, str] = {}
        entity_by_column: dict[str, str] = {}
        if concepts and semantic_edges:
            concept_name_map = {c.id: c.name for c in concepts}
            entity_name_map = {e.id: e.name for e in (entities or [])}
            for edge in semantic_edges:
                if edge.type == EdgeType.REPRESENTS:
                    cname = concept_name_map.get(edge.target_id, "")
                    if cname:
                        concept_by_column[edge.source_id] = cname
                elif edge.type == EdgeType.HAS_CONCEPT:
                    cname = concept_name_map.get(edge.target_id, "")
                    ename = entity_name_map.get(edge.source_id, "")
                    if cname and ename:
                        for e2 in semantic_edges:
                            if e2.type == EdgeType.REPRESENTS and e2.target_id == edge.target_id:
                                entity_by_column[e2.source_id] = ename

        # Build prompt data for each table
        tables_data_lines = []
        for table in tables_without_comment:
            col_list = column_by_table.get(table.id, [])
            col_summaries = []
            for col, profile in col_list:
                summary = {
                    "name": col.name,
                    "dtype": col.dtype or (profile.dtype if profile else ""),
                    "semantic_type": col.semantic_type or (profile.semantic_type if profile else ""),
                }
                if profile:
                    summary["null_rate"] = round(profile.null_rate, 2)
                    summary["cardinality"] = profile.cardinality
                    if profile.top_values:
                        summary["top_values"] = [tv["value"] for tv in profile.top_values[:3]]

                concept = concept_by_column.get(col.id, "")
                if concept:
                    summary["concept"] = concept
                entity = entity_by_column.get(col.id, "")
                if entity:
                    summary["entity"] = entity

                col_summaries.append(summary)

            table_data = {
                "table_id": table.id,
                "table_name": table.name,
                "row_count": table.row_count,
                "columns": col_summaries,
            }
            tables_data_lines.append(json.dumps(table_data))

        tables_data = "\n".join(tables_data_lines)

        # Call LLM
        prompt = TABLE_COMMENT_PROMPT_TEMPLATE.format(tables_data=tables_data)
        response = self._call_llm(prompt)

        if not response:
            logger.warning("LLM returned no response for table comment generation")
            return {}

        # Parse response
        parsed = self._parse_table_comment_response(response)
        if not parsed:
            logger.warning("Failed to parse LLM table comment response")
            return {}

        # Build result: table_id → comment
        result: dict[str, str] = {}
        for table in tables_without_comment:
            comment = parsed.get(table.id, "")
            if comment and len(comment) <= 200:
                result[table.id] = comment
                table.properties["comment"] = comment
            elif comment:
                result[table.id] = comment[:200].rstrip()
                table.properties["comment"] = comment[:200].rstrip()

        logger.info(f"Generated comments for {len(result)} tables")
        return result

    def _parse_table_comment_response(self, response: str) -> dict[str, str]:
        """Parse the LLM response for table comments.

        Expected format: {"tables": [{"table_id": "...", "comment": "..."}]}
        Uses the same progressive JSON repair as _parse_response.
        Returns dict mapping table_id → comment.
        """
        parsed = self._try_parse_json(response)

        if parsed is None:
            logger.warning(f"Failed to parse table comment response: {response[:200]}")
            return {}

        if "tables" not in parsed:
            logger.warning("Table comment response missing 'tables' key")
            return {}

        result: dict[str, str] = {}
        for item in parsed["tables"]:
            table_id = item.get("table_id", "")
            comment = item.get("comment", "")
            if table_id and comment:
                result[table_id] = comment.strip()

        return result
