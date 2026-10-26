"""Path traversal must apply credential masking to the entire returned graph."""
import json
from types import SimpleNamespace

from datalink.graph.retrieval import GraphRetrieval
from datalink.models.edge import Edge, EdgeType


def test_path_masking_covers_nodes_edges_and_opt_out_without_changing_traversal():
    source = "table:postgresql://audit_user:fake-secret@localhost:5432/db:a"
    target = "table:postgresql://audit_user:fake-secret@localhost:5432/db:b"
    edge = Edge(id=f"edge:fk:{source}->{target}", source_id=source, target_id=target,
                type=EdgeType.FOREIGN_KEY, confidence=0.8)
    retrieval = GraphRetrieval.__new__(GraphRetrieval)
    retrieval._id_mapping = {}
    retrieval.storage = SimpleNamespace(
        get_node=lambda node: object() if node in (source, target) else None,
        get_edges_for_node=lambda node: [edge] if node in (source, target) else [],
        search_nodes_by_name=lambda *args: [],
    )
    masked = retrieval.find_paths(source, target)
    assert len(masked) == 1 and masked[0]["length"] == 1 and masked[0]["confidence"] == 0.8
    assert "fake-secret" not in json.dumps(masked) and "audit_user" not in json.dumps(masked)
    raw = retrieval.find_paths(source, target, mask_credential=False)
    assert raw[0]["nodes"] == [source, target] and raw[0]["edges"][0]["id"] == edge.id
    assert retrieval.find_paths(source, target, edge_types=[EdgeType.CONTAINS]) == []
    assert retrieval.find_paths(source, target, max_depth=0) == []
    assert retrieval.find_paths(source, "missing") == []
    assert edge.source_id == source  # No mutation of stored identifiers.
