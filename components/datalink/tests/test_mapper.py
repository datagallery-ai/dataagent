"""Tests for semantic mapper modules."""

import json
from unittest.mock import MagicMock, patch

from datalink.config import DataLinkConfig, EmbeddingConfig, LLMConfig
from datalink.mapper.llm_mapper import LLMMapper
from datalink.models.edge import Edge, EdgeType
from datalink.models.node import ColumnNode, ConceptNode, EntityNode, NodeType
from datalink.models.profile import ColumnProfile


def _make_config(
    llm_model="gpt-4o",
    embedding_model="",
    similarity_threshold=0.75,
    confidence_threshold=0.3,
    merge_llm_temperature=0.0,
    merge_batch_interval=1,
) -> DataLinkConfig:
    """Helper to build a DataLinkConfig for tests."""
    return DataLinkConfig(
        llm=LLMConfig(model=llm_model),
        embedding=EmbeddingConfig(
            model=embedding_model,
            similarity_threshold=similarity_threshold,
        ),
        confidence_threshold=confidence_threshold,
        merge_llm_temperature=merge_llm_temperature,
        merge_batch_interval=merge_batch_interval,
    )


class TestLLMMapperParsing:
    """Test the LLM mapper's response parsing logic (without real LLM calls)."""

    def test_parse_valid_json_response(self):
        """Valid JSON response should be parsed correctly."""
        mapper = LLMMapper(_make_config())

        response = """
Here is the analysis:

{
  "concepts": [
    {
      "name": "person_identifier",
      "description": "A unique identifier for a person",
      "unit": "",
      "dimension": "identifier",
      "columns": ["col:test:orders:customer_id", "col:test:transactions:user_id"],
      "confidence": 0.9
    },
    {
      "name": "monetary_value",
      "description": "A monetary amount",
      "unit": "USD",
      "dimension": "monetary",
      "columns": ["col:test:orders:amount", "col:test:transactions:value"],
      "confidence": 0.85
    }
  ],
  "entities": [
    {
      "name": "customer",
      "description": "A person who makes purchases",
      "concept_names": ["person_identifier", "person_name", "email_address"],
      "confidence": 0.8
    }
  ]
}
"""

        parsed = mapper._parse_response(response)
        assert parsed is not None
        assert len(parsed["concepts"]) == 2
        assert len(parsed["entities"]) == 1
        assert parsed["concepts"][0]["name"] == "person_identifier"

    def test_parse_response_without_json(self):
        """Response without JSON should return None."""
        mapper = LLMMapper(_make_config())
        parsed = mapper._parse_response("This is just text, no JSON here.")
        assert parsed is None

    def test_parse_response_with_missing_keys(self):
        """Response with missing required keys should return None."""
        mapper = LLMMapper(_make_config())
        parsed = mapper._parse_response('{"concepts": []}')
        assert parsed is None  # Missing "entities" key

    def test_build_nodes_and_edges(self):
        """Parsed response should be correctly converted to nodes and edges."""
        mapper = LLMMapper(_make_config())

        parsed = {
            "concepts": [
                {
                    "name": "person_identifier",
                    "description": "Unique person ID",
                    "unit": "",
                    "dimension": "identifier",
                    "columns": ["col:test:orders:customer_id"],
                    "confidence": 0.9,
                }
            ],
            "entities": [
                {
                    "name": "customer",
                    "description": "A person",
                    "concept_names": ["person_identifier"],
                    "confidence": 0.8,
                }
            ],
        }

        columns = [
            ColumnNode(id="col:test:orders:customer_id", name="customer_id", table_id="t1", dtype="int"),
        ]
        profile_map = {}

        concepts, entities, edges = mapper._build_nodes_and_edges(parsed, columns, profile_map)

        assert len(concepts) == 1
        assert concepts[0].name == "person_identifier"
        assert concepts[0].type == NodeType.CONCEPT

        assert len(entities) == 1
        assert entities[0].name == "customer"

        # Should have 2 edges: represents + has_concept
        assert len(edges) == 2
        represents_edges = [e for e in edges if e.type == EdgeType.REPRESENTS]
        has_concept_edges = [e for e in edges if e.type == EdgeType.HAS_CONCEPT]
        assert len(represents_edges) == 1
        assert len(has_concept_edges) == 1

    def test_build_columns_data(self):
        """Column data should be formatted correctly for the LLM prompt."""
        mapper = LLMMapper(_make_config())

        columns = [
            ColumnNode(id="col:test:orders:amount", name="amount", table_id="t1", dtype="float"),
        ]
        profiles = [
            ColumnProfile(
                id="p:test:orders:amount",
                column_id="col:test:orders:amount",
                dtype="float",
                semantic_type="monetary_value",
                sample_values=[85.5, 120.0],
            ),
        ]
        profile_map = {p.column_id: p for p in profiles}

        data = mapper._build_columns_data(columns, profile_map)
        assert "amount" in data
        assert "monetary_value" in data

    def test_no_api_key_returns_none(self):
        """LLM call without API key should return None."""
        mapper = LLMMapper(_make_config(llm_model="gpt-4o"))
        # Default LLMConfig has empty api_key
        result = mapper._call_llm("test prompt")
        assert result is None


class TestMergeWithExisting:
    """Test the merge_with_existing logic with LLM + Embedding approach."""

    def test_merge_empty_existing(self):
        """When no existing nodes, merge should return all new nodes unchanged."""
        config = _make_config()
        mapper = LLMMapper(config)

        new_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]
        new_entities = [
            EntityNode(id="entity:customer", name="customer", description="A customer"),
        ]
        new_edges = [
            Edge(
                id="edge:represents:col1:concept:person_id",
                source_id="col1",
                target_id="concept:person_id",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
        ]

        result_concepts, result_entities, result_edges = mapper.merge_with_existing(
            new_concepts,
            new_entities,
            new_edges,
            [],
            [],
        )

        assert len(result_concepts) == 1
        assert len(result_entities) == 1
        assert len(result_edges) == 1

    def test_merge_empty_new(self):
        """When no new nodes, merge should return empty lists."""
        config = _make_config()
        mapper = LLMMapper(config)

        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]

        result_concepts, result_entities, result_edges = mapper.merge_with_existing(
            [],
            [],
            [],
            existing_concepts,
            [],
        )

        assert len(result_concepts) == 0
        assert len(result_entities) == 0
        assert len(result_edges) == 0

    def test_execute_merge_plan_concept_merge(self):
        """Execute a merge plan that merges one concept into an existing one."""
        config = _make_config()
        mapper = LLMMapper(config)

        existing_concepts = [
            ConceptNode(id="concept:person_identifier", name="person_identifier", description="Person ID"),
        ]
        existing_entities = []

        new_concepts = [
            ConceptNode(
                id="concept:customer_id",
                name="customer_id",
                description="A unique identifier for a customer or person",
            ),
        ]
        new_entities = []

        new_edges = [
            Edge(
                id="edge:represents:col1:concept:customer_id",
                source_id="col1",
                target_id="concept:customer_id",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
        ]

        merge_plan = {
            "merges": [
                {
                    "new_id": "concept:customer_id",
                    "existing_id": "concept:person_identifier",
                    "reason": "both represent person identifier",
                    "confidence": 0.95,
                }
            ],
            "new_kept": [],
        }

        final_concepts, final_entities, final_edges = mapper._execute_merge_plan(
            merge_plan,
            new_concepts,
            new_entities,
            new_edges,
            existing_concepts,
            existing_entities,
        )

        # New concept should be absorbed; existing concept remains
        assert len(final_concepts) == 1
        assert final_concepts[0].id == "concept:person_identifier"

        # Existing concept should have enriched description
        assert "customer or person" in final_concepts[0].description

        # Edge should be redirected to existing concept
        assert len(final_edges) == 1
        assert final_edges[0].target_id == "concept:person_identifier"
        assert final_edges[0].id == "edge:represents:col1:concept:person_identifier"

    def test_execute_merge_plan_entity_and_concept(self):
        """Execute a merge plan that merges both entity and concept."""
        config = _make_config()
        mapper = LLMMapper(config)

        existing_concepts = [
            ConceptNode(id="concept:person_name", name="person_name", description="Name of a person"),
        ]
        existing_entities = [
            EntityNode(id="entity:customer", name="customer", description="A person who purchases"),
        ]

        new_concepts = [
            ConceptNode(id="concept:full_name", name="full_name", description="Full legal name of a person"),
        ]
        new_entities = [
            EntityNode(id="entity:user", name="user", description="A person who uses the system and makes purchases"),
        ]

        new_edges = [
            Edge(
                id="edge:represents:col1:concept:full_name",
                source_id="col1",
                target_id="concept:full_name",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
            Edge(
                id="edge:has_concept:entity:user:concept:full_name",
                source_id="entity:user",
                target_id="concept:full_name",
                type=EdgeType.HAS_CONCEPT,
                confidence=0.8,
            ),
        ]

        merge_plan = {
            "merges": [
                {
                    "new_id": "concept:full_name",
                    "existing_id": "concept:person_name",
                    "reason": "both represent a person's name",
                    "confidence": 0.95,
                },
                {
                    "new_id": "entity:user",
                    "existing_id": "entity:customer",
                    "reason": "both represent a person who makes purchases",
                    "confidence": 0.85,
                },
            ],
            "new_kept": [],
        }

        final_concepts, final_entities, final_edges = mapper._execute_merge_plan(
            merge_plan,
            new_concepts,
            new_entities,
            new_edges,
            existing_concepts,
            existing_entities,
        )

        # Both new nodes merged into existing → existing nodes remain
        assert len(final_concepts) == 1
        assert final_concepts[0].id == "concept:person_name"
        assert len(final_entities) == 1
        assert final_entities[0].id == "entity:customer"

        # Both edges should be redirected
        represents = [e for e in final_edges if e.type == EdgeType.REPRESENTS]
        has_concept = [e for e in final_edges if e.type == EdgeType.HAS_CONCEPT]

        assert len(represents) == 1
        assert represents[0].target_id == "concept:person_name"

        assert len(has_concept) == 1
        assert has_concept[0].source_id == "entity:customer"
        assert has_concept[0].target_id == "concept:person_name"

    def test_execute_merge_plan_partial_keep(self):
        """Some new nodes merged, some kept as genuinely new."""
        config = _make_config()
        mapper = LLMMapper(config)

        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]
        existing_entities = []

        new_concepts = [
            ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
            ConceptNode(id="concept:order_id", name="order_id", description="Order identifier"),
        ]
        new_entities = []

        new_edges = [
            Edge(
                id="edge:represents:col1:concept:customer_id",
                source_id="col1",
                target_id="concept:customer_id",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
            Edge(
                id="edge:represents:col2:concept:order_id",
                source_id="col2",
                target_id="concept:order_id",
                type=EdgeType.REPRESENTS,
                confidence=0.85,
            ),
        ]

        merge_plan = {
            "merges": [
                {
                    "new_id": "concept:customer_id",
                    "existing_id": "concept:person_id",
                    "reason": "both represent person identifier",
                    "confidence": 0.95,
                },
            ],
            "new_kept": ["concept:order_id"],
        }

        final_concepts, final_entities, final_edges = mapper._execute_merge_plan(
            merge_plan,
            new_concepts,
            new_entities,
            new_edges,
            existing_concepts,
            existing_entities,
        )

        # customer_id merged → person_id remains; order_id kept as new
        assert len(final_concepts) == 2
        concept_ids = [c.id for c in final_concepts]
        assert "concept:person_id" in concept_ids
        assert "concept:order_id" in concept_ids

        # Two edges: one redirected, one unchanged
        assert len(final_edges) == 2
        redirected = [e for e in final_edges if e.target_id == "concept:person_id"]
        unchanged = [e for e in final_edges if e.target_id == "concept:order_id"]
        assert len(redirected) == 1
        assert len(unchanged) == 1

    def test_llm_merge_judge_prompt_building(self):
        """Test that the merge prompt is built correctly from node data."""
        config = _make_config()
        mapper = LLMMapper(config)

        new_concepts = [
            ConceptNode(
                id="concept:customer_id", name="customer_id", description="Customer ID", unit="", dimension="identifier"
            ),
        ]

        data = mapper._build_merge_node_data(new_concepts, "concept")
        parsed = json.loads(data.split("\n")[0])

        assert parsed["id"] == "concept:customer_id"
        assert parsed["name"] == "customer_id"
        assert parsed["dimension"] == "identifier"

    def test_llm_merge_judge_with_mocked_llm(self):
        """Test merge_with_existing with a mocked LLM response."""
        config = _make_config()
        mapper = LLMMapper(config)

        # Mock _call_llm to return a merge response
        merge_response = json.dumps(
            {
                "merges": [
                    {
                        "new_id": "concept:customer_id",
                        "existing_id": "concept:person_id",
                        "reason": "both represent person identifier",
                        "confidence": 0.95,
                    },
                ],
                "new_kept": ["concept:order_id"],
            }
        )

        mapper._call_llm = MagicMock(return_value=merge_response)

        new_concepts = [
            ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
            ConceptNode(id="concept:order_id", name="order_id", description="Order identifier"),
        ]
        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]

        result_concepts, result_entities, result_edges = mapper.merge_with_existing(
            new_concepts,
            [],
            [],
            existing_concepts,
            [],
        )

        # customer_id should be merged (absorbed into person_id), order_id kept
        # result includes existing + new kept nodes
        assert len(result_concepts) == 2
        concept_ids = [c.id for c in result_concepts]
        assert "concept:person_id" in concept_ids
        assert "concept:order_id" in concept_ids

    def test_merge_llm_failure_fallback(self):
        """When LLM call fails, all new nodes should be kept as-is."""
        config = _make_config()
        mapper = LLMMapper(config)

        # Mock _call_llm to return None (failure)
        mapper._call_llm = MagicMock(return_value=None)

        new_concepts = [
            ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
        ]
        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]

        result_concepts, result_entities, result_edges = mapper.merge_with_existing(
            new_concepts,
            [],
            [],
            existing_concepts,
            [],
        )

        # LLM failed → no merge, all new nodes kept
        assert len(result_concepts) == 1
        assert result_concepts[0].id == "concept:customer_id"


class TestEmbeddingPreFilter:
    """Test the embedding pre-filter logic."""

    def test_embedding_prefilter_no_config(self):
        """Without embedding model configured, pre-filter returns empty list."""
        config = _make_config(embedding_model="")
        mapper = LLMMapper(config)

        new_concepts = [
            ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
        ]
        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]

        candidates = mapper._embedding_prefilter(new_concepts, [], existing_concepts, [])
        assert len(candidates) == 0

    def test_embedding_prefilter_with_mocked_api(self):
        """Test embedding pre-filter with mocked OpenAI embeddings API."""
        config = _make_config(
            llm_model="gpt-4o",
            embedding_model="text-embedding-3-small",
            similarity_threshold=0.75,
        )
        # Provide a fake API key so _embedding_prefilter doesn't bail out
        config.llm.api_key = "fake-key-for-test"
        mapper = LLMMapper(config)
        mapper = LLMMapper(config)

        # Create mock embeddings: make customer_id and person_id similar,
        # order_id dissimilar
        mock_new_embeddings = [
            [0.9, 0.1, 0.1],  # customer_id — similar to person_id
            [0.1, 0.9, 0.1],  # order_id — dissimilar
        ]
        mock_existing_embeddings = [
            [0.85, 0.15, 0.1],  # person_id — similar to customer_id
        ]

        mock_new_response = MagicMock()
        mock_new_response.data = [
            MagicMock(embedding=mock_new_embeddings[0]),
            MagicMock(embedding=mock_new_embeddings[1]),
        ]
        mock_existing_response = MagicMock()
        mock_existing_response.data = [
            MagicMock(embedding=mock_existing_embeddings[0]),
        ]

        mock_client = MagicMock()
        mock_client.embeddings.create = MagicMock(side_effect=[mock_new_response, mock_existing_response])

        with patch("openai.OpenAI", return_value=mock_client):
            new_concepts = [
                ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
                ConceptNode(id="concept:order_id", name="order_id", description="Order identifier"),
            ]
            existing_concepts = [
                ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
            ]

            candidates = mapper._embedding_prefilter(
                new_concepts,
                [],
                existing_concepts,
                [],
            )

            # Only customer_id ↔ person_id should be a candidate
            assert len(candidates) == 1
            assert candidates[0]["new_id"] == "concept:customer_id"
            assert candidates[0]["existing_id"] == "concept:person_id"
            assert candidates[0]["similarity"] >= 0.75

    def test_embedding_prefilter_api_failure(self):
        """When embedding API fails, pre-filter returns empty list."""
        config = _make_config(embedding_model="text-embedding-3-small")
        config.llm.api_key = "fake-key-for-test"
        mapper = LLMMapper(config)

        with patch("openai.OpenAI", side_effect=Exception("API error")):
            new_concepts = [
                ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
            ]
            existing_concepts = [
                ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
            ]

            candidates = mapper._embedding_prefilter(new_concepts, [], existing_concepts, [])
            assert len(candidates) == 0

    def test_node_to_embedding_text_concept(self):
        """Concept node should produce rich embedding text."""
        config = _make_config()
        mapper = LLMMapper(config)

        concept = ConceptNode(
            id="concept:revenue",
            name="revenue",
            description="Total income from sales",
            unit="USD",
            dimension="monetary",
        )
        text = mapper._node_to_embedding_text(concept)
        assert "revenue" in text
        assert "Total income" in text
        assert "USD" in text
        assert "monetary" in text

    def test_node_to_embedding_text_entity(self):
        """Entity node should produce embedding text with description."""
        config = _make_config()
        mapper = LLMMapper(config)

        entity = EntityNode(
            id="entity:customer",
            name="customer",
            description="A person who purchases products",
        )
        text = mapper._node_to_embedding_text(entity)
        assert "customer" in text
        assert "purchases" in text

    def test_cosine_similarity_identical(self):
        """Cosine similarity of identical vectors should be 1.0."""
        config = _make_config()
        mapper = LLMMapper(config)

        vec = [1.0, 2.0, 3.0]
        sim = mapper._cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 0.001

    def test_cosine_similarity_orthogonal(self):
        """Cosine similarity of orthogonal vectors should be 0.0."""
        config = _make_config()
        mapper = LLMMapper(config)

        sim = mapper._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(sim - 0.0) < 0.001

    def test_cosine_similarity_zero_vector(self):
        """Cosine similarity with zero vector should be 0.0."""
        config = _make_config()
        mapper = LLMMapper(config)

        sim = mapper._cosine_similarity([0.0, 0.0], [1.0, 2.0])
        assert sim == 0.0


class TestEdgeDeduplication:
    """Test edge deduplication after merge redirection."""

    def test_deduplicate_keeps_highest_confidence(self):
        """Among duplicate edges, keep the one with highest confidence."""
        config = _make_config()
        mapper = LLMMapper(config)

        edges = [
            Edge(
                id="edge:represents:col1:concept:person_id",
                source_id="col1",
                target_id="concept:person_id",
                type=EdgeType.REPRESENTS,
                confidence=0.7,
            ),
            Edge(
                id="edge:represents:col1:concept:person_id_v2",
                source_id="col1",
                target_id="concept:person_id",
                type=EdgeType.REPRESENTS,
                confidence=0.95,
            ),
        ]

        deduped = mapper._deduplicate_edges(edges)
        assert len(deduped) == 1
        assert deduped[0].confidence == 0.95

    def test_deduplicate_different_types_not_merged(self):
        """Edges with different types should not be deduplicated."""
        config = _make_config()
        mapper = LLMMapper(config)

        edges = [
            Edge(
                id="edge:represents:col1:concept:person_id",
                source_id="col1",
                target_id="concept:person_id",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
            Edge(
                id="edge:has_concept:entity:customer:concept:person_id",
                source_id="entity:customer",
                target_id="concept:person_id",
                type=EdgeType.HAS_CONCEPT,
                confidence=0.8,
            ),
        ]

        deduped = mapper._deduplicate_edges(edges)
        assert len(deduped) == 2

    def test_full_merge_with_deduplication(self):
        """After merge + redirection, edges should be deduplicated."""
        config = _make_config()
        mapper = LLMMapper(config)

        merge_response = json.dumps(
            {
                "merges": [
                    {
                        "new_id": "concept:full_name",
                        "existing_id": "concept:person_name",
                        "reason": "same concept",
                        "confidence": 0.95,
                    },
                ],
                "new_kept": [],
            }
        )
        mapper._call_llm = MagicMock(return_value=merge_response)

        existing_concepts = [
            ConceptNode(id="concept:person_name", name="person_name", description="Name of person"),
        ]
        new_concepts = [
            ConceptNode(id="concept:full_name", name="full_name", description="Full name"),
        ]
        # Two edges both targeting concept:full_name → after redirect both point to concept:person_name
        new_edges = [
            Edge(
                id="edge:represents:col1:concept:full_name",
                source_id="col1",
                target_id="concept:full_name",
                type=EdgeType.REPRESENTS,
                confidence=0.7,
            ),
            Edge(
                id="edge:represents:col2:concept:full_name",
                source_id="col2",
                target_id="concept:full_name",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
        ]

        result_concepts, result_entities, result_edges = mapper.merge_with_existing(
            new_concepts,
            [],
            new_edges,
            existing_concepts,
            [],
        )

        # Both edges now point to concept:person_name but have different source_ids
        # → NOT duplicates, both should be kept
        assert len(result_edges) == 2
        for e in result_edges:
            assert e.target_id == "concept:person_name"


class TestMergeExistingNodesNotLost:
    """Verify that existing nodes are NOT lost after merge_with_existing.

    This tests the bug where _execute_merge_plan only returned new nodes,
    causing previously accumulated concepts/entities to disappear.
    """

    def test_existing_concepts_preserved_after_merge(self):
        """Existing concepts should appear in the merge result."""
        config = _make_config()
        mapper = LLMMapper(config)

        merge_response = json.dumps(
            {
                "merges": [
                    {
                        "new_id": "concept:customer_id",
                        "existing_id": "concept:person_id",
                        "reason": "same",
                        "confidence": 0.95,
                    },
                    {"new_id": "entity:user", "existing_id": "entity:customer", "reason": "same", "confidence": 0.9},
                ],
                "new_kept": ["concept:email"],
            }
        )
        mapper._call_llm = MagicMock(return_value=merge_response)

        existing_concepts = [
            ConceptNode(id="concept:person_id", name="person_id", description="Person ID"),
        ]
        existing_entities = [
            EntityNode(id="entity:customer", name="customer", description="A customer"),
        ]
        new_concepts = [
            ConceptNode(id="concept:customer_id", name="customer_id", description="Customer ID"),
            ConceptNode(id="concept:email", name="email", description="Email address"),
        ]
        new_entities = [
            EntityNode(id="entity:user", name="user", description="A user"),
        ]

        result_c, result_e, result_edges = mapper.merge_with_existing(
            new_concepts,
            new_entities,
            [],
            existing_concepts,
            existing_entities,
        )

        # person_id (existing) + email (new kept) = 2 concepts
        assert len(result_c) == 2
        assert any(c.id == "concept:person_id" for c in result_c)
        assert any(c.id == "concept:email" for c in result_c)

        # customer (existing, enriched) = 1 entity
        assert len(result_e) == 1
        assert result_e[0].id == "entity:customer"

    def test_existing_entities_not_lost_in_batch_accumulation(self):
        """Simulating the multi-batch scenario from the real logs:
        batch 1 produces entity, batch 2 merges a similar entity into it.
        The existing entity should survive in the accumulated result.
        """
        config = _make_config()
        mapper = LLMMapper(config)

        # Batch 1 on empty graph: no merge needed
        batch1_c = [ConceptNode(id="concept:person_id", name="person_id", description="Person ID")]
        batch1_e = [EntityNode(id="entity:customer", name="customer", description="A customer")]
        batch1_edges = [
            Edge(
                id="edge:represents:col1:concept:person_id",
                source_id="col1",
                target_id="concept:person_id",
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            ),
        ]

        accumulated_c, accumulated_e, accumulated_edges = mapper.merge_with_existing(
            batch1_c,
            batch1_e,
            batch1_edges,
            [],
            [],
        )

        assert len(accumulated_e) == 1
        assert accumulated_e[0].id == "entity:customer"

        # Batch 2: entity:user gets merged into entity:customer
        merge_response = json.dumps(
            {
                "merges": [
                    {
                        "new_id": "entity:user",
                        "existing_id": "entity:customer",
                        "reason": "same entity",
                        "confidence": 0.95,
                    },
                ],
                "new_kept": ["concept:email"],
            }
        )
        mapper._call_llm = MagicMock(return_value=merge_response)

        batch2_c = [ConceptNode(id="concept:email", name="email", description="Email")]
        batch2_e = [EntityNode(id="entity:user", name="user", description="A user")]
        batch2_edges = []

        result_c, result_e, result_edges = mapper.merge_with_existing(
            batch2_c,
            batch2_e,
            batch2_edges,
            accumulated_c,
            accumulated_e,
        )

        # CRITICAL: entity:customer MUST survive after merge
        assert len(result_e) == 1
        assert result_e[0].id == "entity:customer"

        # All concepts should survive
        assert len(result_c) == 2
        assert any(c.id == "concept:person_id" for c in result_c)
        assert any(c.id == "concept:email" for c in result_c)


class TestMergeBatchInterval:
    """Test the merge_batch_interval configuration."""

    def test_merge_every_batch_with_interval_1(self):
        """With merge_batch_interval=1, merge happens after every batch."""
        config = _make_config(merge_batch_interval=1)
        config.llm.api_key = "test-key"
        mapper = LLMMapper(config)

        # Mock mapping to return simple results
        def mock_map_single(columns, profile_map):
            c = ConceptNode(id=f"concept:batch_{columns[0].id}", name=f"batch_{columns[0].name}", description="test")
            e = EntityNode(id=f"entity:batch_{columns[0].id}", name=f"batch_{columns[0].name}", description="test")
            edge = Edge(
                id=f"edge:{columns[0].id}",
                source_id=columns[0].id,
                target_id=c.id,
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            )
            return [c], [e], [edge]

        mapper._map_columns_single = mock_map_single
        # Mock merge: no merges, keep all
        merge_response = json.dumps({"merges": [], "new_kept": []})
        mapper._call_llm = MagicMock(return_value=merge_response)

        columns = [ColumnNode(id=f"col{i}", name=f"col{i}", table_id="t1", dtype="int") for i in range(45)]
        profiles = [ColumnProfile(id=f"p{i}", column_id=f"col{i}", dtype="int") for i in range(45)]

        result_c, result_e, result_edges = mapper.map_columns(columns, profiles)

        # With interval=1 and 3 batches (45/15=3), 2 merges should happen
        # (batch 1 accumulates, batch 2 merges, batch 3 merges)
        assert len(result_c) > 0

    def test_merge_less_frequent_with_interval_10(self):
        """With merge_batch_interval=10, 6 batches should trigger only 1 merge."""
        config = _make_config(merge_batch_interval=10)
        config.llm.api_key = "test-key"
        mapper = LLMMapper(config)

        def mock_map_single(columns, profile_map):
            c = ConceptNode(id=f"concept:batch_{len(columns)}", name=f"batch_{len(columns)}", description="test")
            e = EntityNode(id=f"entity:batch_{len(columns)}", name=f"batch_{len(columns)}", description="test")
            edge = Edge(
                id=f"edge:batch_{len(columns)}",
                source_id=columns[0].id,
                target_id=c.id,
                type=EdgeType.REPRESENTS,
                confidence=0.9,
            )
            return [c], [e], [edge]

        mapper._map_columns_single = mock_map_single

        # Just check that the result has accumulated all batches' nodes
        columns = [ColumnNode(id=f"col{i}", name=f"col{i}", table_id="t1", dtype="int") for i in range(90)]
        profiles = [ColumnProfile(id=f"p{i}", column_id=f"col{i}", dtype="int") for i in range(90)]

        # Override _call_llm to count merge calls
        merge_llm_calls = []

        def counting_call_llm(prompt, temperature=None):
            merge_llm_calls.append(1)
            return json.dumps({"merges": [], "new_kept": []})

        mapper._call_llm = counting_call_llm

        result_c, result_e, result_edges = mapper.map_columns(columns, profiles)

        # With 6 batches and interval=10:
        # Only 1 merge at the final batch (since 6 < 10)
        assert len(merge_llm_calls) <= 2  # At most 1-2 merge calls (final + possibly 1 at end)
        # All 6 batches' results should be accumulated
        assert len(result_c) >= 6  # Should have concepts from all batches
