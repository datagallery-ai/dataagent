# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""UT: MemoryFactory caches per ConfigManager instance, not only rag_id."""

import pytest
from dataagent.common_utils.knowledge_base.memory import MemoryFactory, _memory_instance_cache_key

from dataagent.config import ConfigManager


def test_get_memory_requires_config_manager():
    """Agent runtime paths must not fall back to the module-level config singleton."""
    with pytest.raises(RuntimeError, match="per-Agent ConfigManager"):
        MemoryFactory.get_memory(rag_id="test_rag", config_manager=None)


def test_memory_factory_cache_key_differs_by_config_manager_instance():
    """Same rag_id with different Agent ConfigManager must not share one cache key."""
    cm_a = ConfigManager()
    cm_b = ConfigManager()
    key_a = _memory_instance_cache_key("ecommerce", "", cm_a)
    key_b = _memory_instance_cache_key("ecommerce", "", cm_b)
    assert key_a != key_b


def test_memory_factory_returns_distinct_instances_for_different_config_managers(monkeypatch):
    """Two ConfigManagers with different MEMORY settings must not return the same Memory object."""
    MemoryFactory.clear_all_memories()

    cm_a = ConfigManager()
    cm_a._config = {
        "MEMORY": {
            "enabled": False,
            "index_prefix": "a",
            "path_prefix": "/tmp/a",
        }
    }
    cm_b = ConfigManager()
    cm_b._config = {
        "MEMORY": {
            "enabled": False,
            "index_prefix": "b",
            "path_prefix": "/tmp/b",
        }
    }

    mem_a = MemoryFactory.get_memory(rag_id="same_rag", config_manager=cm_a)
    mem_b = MemoryFactory.get_memory(rag_id="same_rag", config_manager=cm_b)
    assert mem_a is not mem_b
    assert mem_a.index_prefix != mem_b.index_prefix

    MemoryFactory.clear_all_memories()


def test_clear_user_memory_removes_instances_by_rag_id():
    """clear_user_memory must delete tuple-keyed cache entries matching rag_id."""
    MemoryFactory.clear_all_memories()

    cm = ConfigManager()
    cm._config = {
        "MEMORY": {
            "enabled": False,
            "index_prefix": "rag_a",
            "path_prefix": "/tmp/a",
        }
    }
    mem_a = MemoryFactory.get_memory(rag_id="user_scope_1", config_manager=cm)
    assert mem_a is not None

    cm_other = ConfigManager()
    cm_other._config = {
        "MEMORY": {
            "enabled": False,
            "index_prefix": "rag_b",
            "path_prefix": "/tmp/b",
        }
    }
    mem_b = MemoryFactory.get_memory(rag_id="other_scope", config_manager=cm_other)
    assert mem_b is not None

    MemoryFactory.clear_user_memory("user_scope_1")

    mem_after = MemoryFactory.get_memory(rag_id="user_scope_1", config_manager=cm)
    assert mem_after is not mem_a
    mem_b_again = MemoryFactory.get_memory(rag_id="other_scope", config_manager=cm_other)
    assert mem_b_again is mem_b

    MemoryFactory.clear_all_memories()
