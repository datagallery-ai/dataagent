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
import os
from dataclasses import dataclass
from unittest.mock import patch

import pytest

pytest.importorskip("elasticsearch", reason="requires dataagent[all]")

from loguru import logger

from dataagent.actions.perceptor.perceptor_atomic import (
    extract_keywords,
    perceive_data_from_ontology,
    perceive_knowledge_from_memory,
    perceive_metadata_from_memory,
)
from dataagent.actions.tools.context import ToolExecutionContext
from dataagent.config.config_manager import ConfigManager
from dataagent.core.managers.action_manager.manager import ToolManager
from dataagent.core.managers.llm_manager import llm_manager

_tool_manager_stub = ToolManager()
_tool_manager_stub.enable_auto_discover = lambda: None

_ut_config_manager: ConfigManager | None = None


def _ut_tool_context() -> ToolExecutionContext:
    """Build ToolExecutionContext from UT fixture config (isolated reload in setup_class)."""
    if _ut_config_manager is None:
        raise RuntimeError("Perceptor UT config_manager is not initialized; run TestPerceptor.setup_class first.")
    return ToolExecutionContext(config_manager=_ut_config_manager)


def parent_dir(path: str, levels: int = 1):
    for _ in range(levels):
        path = os.path.dirname(path)
    return path


@dataclass
class _FakeLLMResponse:
    content: str


class _FakeLLM:
    """A tiny fake LLM that matches the adapter surface used by `extract_keywords`."""

    def invoke(self, _messages):
        # Format expected by `extract_keywords`:
        # each line should contain ": " and " - ", and keywords separated by "|"
        content = "\n".join(
            [
                "实体: 订单 - 客户|订单|金额|高购买力",
                "工具: 分析 - report_generator|natural_language_to_plot",
            ]
        )
        return _FakeLLMResponse(content=content)


class _FakeMemory:
    def __init__(self):
        # Minimal metadata structure consumed by `perceive_metadata_from_memory`
        path = "/mock/db/Ecommerce"
        self.metadata = type("Meta", (), {})()
        self.metadata.metadata = {
            path: {
                "id": 1,
                "file_description": "Mock ecommerce table",
                "schema": {
                    "customer_id": {
                        "id": 101,
                        "schema_description": "customer id",
                        "relationship": [],
                    },
                    "amount": {
                        "id": 102,
                        "schema_description": "order amount",
                        "relationship": [],
                    },
                },
            }
        }

    def query_table(self, *, type_, query_text, mode, query_schema, topk, similarity_threshold):
        # Return one "file" hit so the function can enumerate schema columns.
        _ = (query_text, mode, query_schema, topk, similarity_threshold)
        if type_ == "table":
            return [
                {
                    "metadata_content": {
                        "type": "file",
                        "id": 1,
                        "label": "/mock/db/Ecommerce",
                        "description": "Mock ecommerce table",
                        "path": "/mock/db/Ecommerce",
                    }
                }
            ]
        # No extra column hits needed for this UT.
        return []

    def query_knowledge(self, kind, *, query_schema, query_text, mode, topk):
        _ = (query_schema, query_text, mode, topk)
        if kind == "graph_node":
            return [
                {
                    "doc_content": {
                        "id": 1,
                        "label": "Knowledge(Mock)",
                        "description": "mock knowledge description",
                    }
                }
            ]
        if kind == "text":
            return [{"doc_id": 1, "doc_content": {"info": "mock text knowledge"}}]
        return []

    def query_graph(self, _graph, *, type_, sources, depth):
        _ = (type_, sources, depth)
        # Return one node to expand BFS.
        return ({1: {"id": 1, "label": "Knowledge(Mock)", "description": "mock knowledge description"}}, {})


class TestPerceptor:
    """Perceptor原子能力测试"""

    def setup_class(self):
        """构建测试环境"""
        global _ut_config_manager
        # `ecommerce_agent.yaml` contains explicit $env{...} references; set dummy defaults so
        # config interpolation doesn't fail when running without a local `.env`.
        if not os.getenv("MEMORY_LONG_TERM_STORAGE_URL", "").strip():
            os.environ["MEMORY_LONG_TERM_STORAGE_URL"] = "http://127.0.0.1:9200"
        if not os.getenv("MEMORY_SHORT_TERM_STORAGE_URL", "").strip():
            os.environ["MEMORY_SHORT_TERM_STORAGE_URL"] = "sqlite:///./test.db"
        if not os.getenv("DATASOURCE_DATABASE_ADDRESS", "").strip():
            os.environ["DATASOURCE_DATABASE_ADDRESS"] = "mysql+pymysql://user:pass@127.0.0.1:3306/Ecommerce"
        if not os.getenv("EMBEDDING_BASE_URL", "").strip():
            os.environ["EMBEDDING_BASE_URL"] = "http://127.0.0.1:9998/v1"
        if not os.getenv("EMBEDDING_API_KEY", "").strip():
            os.environ["EMBEDDING_API_KEY"] = "test-key"

        yaml_path = os.path.join(
            parent_dir(path=os.path.abspath(__file__), levels=4),
            "dataagent",
            "core",
            "flex",
            "examples",
            "ecommerce_agent.yaml",
        )
        default_yaml_path = os.path.join(
            parent_dir(path=os.path.abspath(__file__), levels=4),
            "dataagent",
            "core",
            "flex",
            "flex_default_configs.yaml",
        )
        _ut_config_manager = ConfigManager()
        _ut_config_manager.reload(yaml_path, default_config_path=default_yaml_path)
        # Avoid any real network/SDK initialization in UT.
        # We patch the two runtime dependencies used by this test module:
        # - default llm (for keyword extraction)
        # - memory backend (for metadata/knowledge retrieval)
        self._orig_get_default_llm = llm_manager.get_default_llm
        llm_manager.get_default_llm = lambda: _FakeLLM()  # type: ignore[assignment]

        from dataagent.common_utils.knowledge_base.memory import MemoryFactory

        self._orig_get_memory = MemoryFactory.get_memory
        MemoryFactory.get_memory = classmethod(
            lambda cls, rag_id=None, path_prefix=None, config_manager=None, **_: _FakeMemory()
        )

        # tool_manager is not needed for these atomic functions; auto-discovery is off by default on stub.

    def teardown_class(self):
        # Restore patched globals to avoid leaking into other tests.
        llm_manager.get_default_llm = self._orig_get_default_llm
        from dataagent.common_utils.knowledge_base.memory import MemoryFactory

        MemoryFactory.get_memory = self._orig_get_memory

    def test_extract_keywords(self):
        query = "请基于订单数据生成一份图文并茂的分析报告，按客户购买总金额排序，鉴别高购买力客户。"
        out = extract_keywords(query=query)
        assert "original_msg" in out and isinstance(out["original_msg"], str)
        assert "frontend_msg" in out and isinstance(out["frontend_msg"], str)
        assert "data" in out and out["data"]

    def test_perceive_metadata_from_memory(self):
        keywords_list = ["客户", "购买", "金额", "高购买力"]
        out = perceive_metadata_from_memory(keywords_list=keywords_list, _tool_context=_ut_tool_context())
        assert "original_msg" in out and isinstance(out["original_msg"], str)
        assert "frontend_msg" in out and isinstance(out["frontend_msg"], str)
        assert "data" in out
        data = out["data"]
        assert isinstance(data, dict)
        assert "table" in data and "column" in data
        assert set(data["table"][0].keys()) == {"description", "label", "path"}
        assert set(data["column"][0].keys()) == {
            "description",
            "from_table",
            "label",
            "supplementary_schemas",
            "values",
        }

    def test_perceive_knowledge_from_memory_real_path(self):
        keywords_list = ["订单", "金额"]
        out = perceive_knowledge_from_memory(keywords_list=keywords_list, topk=3, _tool_context=_ut_tool_context())

        print(f"\n[DEBUG] Memory Real Search Knowledge: {[key.get('label') for key in out['data']['knowledge']]}")

        assert "original_msg" in out and isinstance(out["original_msg"], str)
        assert "frontend_msg" in out and isinstance(out["frontend_msg"], str)
        assert "data" in out and "knowledge" in out["data"]

        knowledge = out["data"]["knowledge"]
        if knowledge:
            first_item = knowledge[0]
            assert set(first_item.keys()) == {
                "label",
                "description",
                "knowledge_type",
                "knowledge_content",
            }

    def test_perceive_knowledge_from_memory_empty_keywords(self):
        out = perceive_knowledge_from_memory(keywords_list=[], _tool_context=_ut_tool_context())

        assert "data" in out and "knowledge" in out["data"]
        # Empty keywords => no retrieved knowledge
        assert out["data"]["knowledge"] == []

    @pytest.mark.skipif(
        not os.getenv("ONTOLOGY_SERVICE_URL"),
        reason="ONTOLOGY_SERVICE_URL not set, skipping real ontology search test",
    )
    def test_perceive_data_from_ontology_real_call(self):
        query = "查询订单金额相关的本体数据"
        ontology_url = os.getenv("ONTOLOGY_SERVICE_URL")

        ctx = _ut_tool_context()
        ctx.config_manager.set("ONTOLOGY_SERVICE_URL", ontology_url)
        out = perceive_data_from_ontology(query=query, _tool_context=ctx)

        logger.debug(f"\n[DEBUG] Ontology Real Call Data: {out.get('data')}")

        assert "original_msg" in out and isinstance(out["original_msg"], str)
        assert "frontend_msg" in out and isinstance(out["frontend_msg"], str)
        assert "data" in out

        if out["original_msg"] == "Ontology query succeeded.":
            assert isinstance(out["data"], (dict, list, str, int, float, bool))
