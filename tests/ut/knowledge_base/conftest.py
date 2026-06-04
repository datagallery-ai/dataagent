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
"""Mock embedding / chat LLM for knowledge_base UT so tests do not call real HTTP APIs."""

import os
from unittest.mock import MagicMock

import numpy as np
import pytest

from dataagent.config.config_manager import ConfigManager
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.llm_manager.llm_config import LLMConfig

_EMBEDDING_MODULES = (
    "dataagent.common_utils.knowledge_base.knowledge_base",
    "dataagent.common_utils.knowledge_base.metadata_management",
    "dataagent.common_utils.knowledge_base.tool_management",
    "dataagent.common_utils.knowledge_base.utils_metadata",
    "dataagent.common_utils.knowledge_base.utils_knowledgebase",
)

_MODEL_INFERENCE_MODULES = (
    "dataagent.common_utils.knowledge_base.knowledge_base",
    "dataagent.common_utils.knowledge_base.memory",
    "dataagent.common_utils.knowledge_base.utils_metadata",
    "dataagent.common_utils.knowledge_base.utils_knowledgebase",
)


def _fake_embedding(query):
    if isinstance(query, list):
        return np.ones((len(query), 1024), dtype=np.float32)
    return np.ones(1024, dtype=np.float32)


def _fake_model_inference(message: str) -> str:
    """Branch on prompt text so json_repair/json.loads paths in KB code stay valid."""
    ml = message.lower()
    if "virtual" in ml and "column" in ml:
        return "[]"
    if "extract_column_relationships" in ml or ("sql" in ml and "relationship" in ml and "column" in ml):
        return "[]"
    if "extract_tool" in ml or ("tool" in ml and "relationship" in ml and "extract" in ml):
        return "[]"
    if any(k in ml for k in ("infer_file_description", "infer_schema_description", "infer_data_type")):
        return "ut-mock-inferred-description"
    if "user_guide" in ml or "user_prompt" in ml:
        return "ut-mock-kb-extraction"
    return "{}"


def _stub_create_llm(config):
    if isinstance(config, dict):
        config = LLMConfig.from_dict(config)
    mock = MagicMock()
    llm_manager.llm_cache[config.name] = {"llm_config": config, "llm_instance": mock}
    return mock


@pytest.fixture
def _kb_ut_no_external_llm(monkeypatch):
    llm_manager.llm_cache.clear()
    monkeypatch.setattr(llm_manager, "create_llm", _stub_create_llm)
    for mod in _EMBEDDING_MODULES:
        monkeypatch.setattr(f"{mod}.embedding", _fake_embedding)
    for mod in _MODEL_INFERENCE_MODULES:
        monkeypatch.setattr(f"{mod}.model_inference", _fake_model_inference)
    yield
    llm_manager.llm_cache.clear()


def build_kb_ut_config_manager() -> ConfigManager:
    """Build an isolated ConfigManager for knowledge_base UT (not the module-level singleton)."""
    cm = ConfigManager()
    cm.set("MEMORY.enabled", True)
    cm.set("MEMORY.long_term_storage.backend", "elasticsearch")
    cm.set("MEMORY.short_term_storage.backend", "sqlite")
    cm.set("MEMORY.embedding_model", "jina_v3")
    cm.set("MODEL.jina_v3.name", "jina_v3")
    cm.set("MODEL.jina_v3.model_type", "embedding")
    cm.set("MODEL.jina_v3.provider", "openai")
    cm.set("MODEL.jina_v3.params.base_url", "http://8.92.9.183:9998/v1")
    cm.set("MODEL.jina_v3.params.model", "jina-v3")
    cm.set("MODEL.deepseek.name", "DEEPSEEK_CHAT")
    cm.set("MODEL.deepseek.model_type", "chat")
    cm.set("MODEL.deepseek.provider", "openai")
    cm.set("MODEL.deepseek.params.base_url", "https://api.deepseek.com")
    cm.set("MODEL.deepseek.params.model", "deepseek-chat")
    return cm


@pytest.fixture
def kb_ut_config_manager(_kb_ut_no_external_llm):
    """Per-test ConfigManager with standard MEMORY/MODEL bindings for knowledge_base UT."""
    cm = build_kb_ut_config_manager()
    llm_manager.init_from_config(cm.get_all())
    return cm


def _elasticsearch_reachable() -> bool:
    try:
        from elasticsearch import Elasticsearch

        url = os.environ.get("MEMORY_UT_ELASTICSEARCH_URL", "http://localhost:9200")
        es = Elasticsearch([url], request_timeout=2)
        return bool(es.ping())
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """无 Elasticsearch 时跳过 memory 集成用例（LLM 已 mock，存储仍需真实 ES/PG）。"""
    if os.environ.get("MEMORY_UT_RUN_WITHOUT_ES", "").lower() in ("1", "true", "yes"):
        return
    if _elasticsearch_reachable():
        return
    skip = pytest.mark.skip(
        reason=(
            "Elasticsearch not reachable; start ES or set MEMORY_UT_ELASTICSEARCH_URL. "
            "MEMORY_UT_RUN_WITHOUT_ES=1 disables this skip (tests still need ES)."
        )
    )
    for item in items:
        nid = item.nodeid.replace("\\", "/")
        if "knowledge_base/test_memory_" in nid:
            item.add_marker(skip)
