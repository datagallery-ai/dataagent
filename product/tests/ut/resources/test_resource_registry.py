# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Unit tests for ResourceCatalog parsing and selection."""

from __future__ import annotations

import pytest

from dataagent.resources import Resource, ResourceCapacity, ResourceCatalog, validate_resources_list


def _local_resource_config(**overrides: object) -> dict[str, object]:
    """Build a minimal local executable resource definition."""
    payload: dict[str, object] = {
        "id": "local",
        "category": "executable",
        "transport": {"type": "local"},
        "operations": {
            "submit": "sandbox.submit",
            "poll": "sandbox.poll",
            "collect": "sandbox.collect",
            "cancel": "sandbox.cancel",
        },
        "capacity": {"total": 4, "unit": "slot"},
        "consumption": {"*": 1},
    }
    payload.update(overrides)
    return payload


def test_validate_resources_list_rejects_executable_without_transport():
    """Executable resources must declare transport.type."""
    item = _local_resource_config()
    del item["transport"]
    with pytest.raises(ValueError, match="transport.type"):
        validate_resources_list([item])


def test_validate_resources_list_rejects_executable_without_operations():
    """Executable resources must declare all four operations."""
    item = _local_resource_config()
    del item["operations"]
    with pytest.raises(ValueError, match="operations requires"):
        validate_resources_list([item])


def test_select_executable_by_resource_id():
    """Explicit resource_id selection succeeds when consumption matches."""
    catalog = ResourceCatalog.from_config({"RESOURCES": [_local_resource_config()]})
    resource, error = catalog.select_executable(resource_id="local", task_type="batch")
    assert error == ""
    assert resource is not None
    assert resource.id == "local"


def test_select_executable_ambiguous_task_type():
    """Multiple resources supporting the same task type require resource_id."""
    catalog = ResourceCatalog.from_config(
        {
            "RESOURCES": [
                _local_resource_config(id="a"),
                _local_resource_config(id="b"),
            ]
        }
    )
    resource, error = catalog.select_executable(task_type="batch")
    assert resource is None
    assert "multiple resources support task type" in error


def test_capacity_snapshot_reflects_reserved_usage():
    """Capacity snapshot reports used counts from the ledger."""
    catalog = ResourceCatalog(resources=[Resource(id="local", name="local", category="executable", capacity=4)])
    capacity = ResourceCapacity(catalog)
    assert capacity.try_reserve(resource_id="local", task_type="resource", job_id="job-1", amount=2).ok
    snapshot = {view.id: view for view in capacity.snapshot()}
    assert snapshot["local"].used == 2
    assert snapshot["local"].available == 2


def test_validate_resources_list_accepts_non_executable_catalog_entry():
    """non-executable resources validate without transport/operations but still need capacity."""
    item = {
        "id": "catalog-only",
        "category": "non-executable",
        "name": "Catalog only",
        "capacity": {"total": 1, "unit": "slot"},
        "consumption": {"*": 1},
    }
    validate_resources_list([item])
    catalog = ResourceCatalog.from_config({"RESOURCES": [item]})
    resource, error = catalog.select_executable(resource_id="catalog-only", task_type="resource")
    assert resource is None
    assert "not executable" in error.lower()


def test_validate_resources_list_rejects_non_executable_with_transport():
    """non-executable resources must not declare transport (scheme A)."""
    item = {
        "id": "bad",
        "category": "non-executable",
        "transport": {"type": "mcp", "url": "https://example.test/mcp"},
        "capacity": {"total": 1, "unit": "slot"},
        "consumption": {"*": 1},
    }
    with pytest.raises(ValueError, match="must not declare transport"):
        validate_resources_list([item])


def test_validate_resources_list_rejects_unknown_mcp_server_field():
    """mcp_server is not a supported RESOURCES field."""
    item = {
        "id": "bad",
        "category": "non-executable",
        "mcp_server": {"url": "https://example.test/mcp"},
        "capacity": {"total": 1, "unit": "slot"},
        "consumption": {"*": 1},
    }
    with pytest.raises(ValueError, match="Unknown fields"):
        validate_resources_list([item])


def test_validate_resources_list_requires_mcp_url_for_executable():
    """Executable MCP resources require transport.url."""
    item = _local_resource_config(
        transport={"type": "mcp"},
        operations={
            "submit": "submit_job",
            "poll": "poll_job",
            "collect": "collect_job",
            "cancel": "cancel_job",
        },
    )
    with pytest.raises(ValueError, match="transport.url"):
        validate_resources_list([item])
