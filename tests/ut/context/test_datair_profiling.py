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
import asyncio
import os

import pytest

from dataagent.config.config_manager import ConfigManager
from dataagent.core.context.context_trajectory import ContextFactory, build_context_init_options
from dataagent.core.managers.llm_manager import llm_manager
from dataagent.core.managers.prompt_manager import PromptTemplate


def parent_dir(path: str, levels: int = 1):
    for _ in range(levels):
        path = os.path.dirname(path)
    return path


class TestDataIRProfiling:
    """Context类接口测试"""

    def setup_class(self):
        """Context中添加基本信息"""
        # This test loads `ecommerce_agent.yaml`, which uses explicit $env{...} interpolation.
        # Provide dummy defaults so `config_manager.reload()` won't fail in CI/local runs without `.env`.
        if not os.getenv("MEMORY_LONG_TERM_STORAGE_URL", "").strip():
            os.environ["MEMORY_LONG_TERM_STORAGE_URL"] = "http://127.0.0.1:9200"
        if not os.getenv("MEMORY_SHORT_TERM_STORAGE_URL", "").strip():
            os.environ["MEMORY_SHORT_TERM_STORAGE_URL"] = "postgresql://user:pass@127.0.0.1:5432/db"
        if not os.getenv("DATASOURCE_DATABASE_ADDRESS", "").strip():
            os.environ["DATASOURCE_DATABASE_ADDRESS"] = "mysql+pymysql://user:pass@127.0.0.1:3306/Ecommerce"
        # ecommerce_agent.yaml chat provider uses "bailian" by default; ensure its env is present for init.
        if not os.getenv("BAILIAN_BASE_URL", "").strip():
            os.environ["BAILIAN_BASE_URL"] = "http://127.0.0.1:9999"
        if not os.getenv("BAILIAN_API_KEY", "").strip():
            os.environ["BAILIAN_API_KEY"] = "test-key"

        # This test loads `ecommerce_agent.yaml`, which defines an embedding model with provider "embedding".
        # The embedding provider reads base_url/api_key from env vars.
        # In CI and local runs without a populated `.env`, these may be empty and cause init to fail.
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
        cm = ConfigManager()
        cm.reload(yaml_path, default_config_path=default_yaml_path)
        llm_manager.init_from_config(cm.get_all())
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer",
            session_id="#00001",
            run_id=0,
            sub_id=0,
            options=build_context_init_options(cm),
        )
        context.register_query(query="12+23等于几?", additional_files=[])

    def teardown_class(self):
        """销毁Context实例"""
        ContextFactory.clear_context()

    @pytest.mark.asyncio
    async def test_profiling(self):
        """测试DataIRProfiling功能"""
        context = ContextFactory.get_context(
            user_id="jiutian_applicationlayer", session_id="#00001", run_id=0, sub_id=0
        )
        context.register_node(
            node_type="Table",
            label="测试表01",
            description="",
            path="测试路径/还是测试路径/测试表01",
            predecessor_node=["Query(query00000)"],
            edge_type="test_data_type",
        )
        context.register_node(
            node_type="Table",
            label="测试表02",
            description="",
            path="测试路径/还是测试路径/测试表02",
            predecessor_node=["Query(query00000)"],
            edge_type="test_data_type",
        )
        context.profiling()
        if context.pending_tasks["profiling"]:
            await asyncio.gather(*context.pending_tasks["profiling"])

        assert context._trajectory.nodes["Table(测试表01)"]["description"]
        assert context._IR._nodes["Table"]["测试表01"].description
        assert context._trajectory.nodes["Table(测试表02)"]["description"]
        assert context._IR._nodes["Table"]["测试表02"].description
