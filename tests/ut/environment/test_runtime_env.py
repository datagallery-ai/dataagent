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
"""Unit tests for runtime_env module."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from dataagent.core.cbb.runtime_env import (
    RuntimeEnvironmentCollector,
    _get_context_window,
    format_runtime_environment,
    format_runtime_environment_section,
)


class TestGetContextWindow:
    """Tests for _get_context_window function."""

    def test_user_config_priority(self):
        """用户配置的context_windows优先级最高。"""
        assert _get_context_window("any-model", {"context_windows": 50000}) == 50000

    def test_default_mapping_match(self):
        """内置映射表模糊匹配。"""
        assert _get_context_window("deepseek-v3.2-latest") == 131072
        assert _get_context_window("qwen-plus-latest") == 1000000

    def test_default_fallback(self):
        """未匹配时不返回默认值（None表示不展示）。"""
        assert _get_context_window("unknown-model") is None

    def test_invalid_user_config(self):
        """无效的用户配置应忽略。"""
        assert _get_context_window("deepseek-v3", {"context_windows": "invalid"}) == 131072

    def test_non_positive_user_config_ignored(self):
        """非正数用户配置应忽略，继续查内置映射。"""
        assert _get_context_window("deepseek-v3", {"context_windows": 0}) == 131072
        assert _get_context_window("deepseek-v3", {"context_windows": -1}) == 131072


class TestRuntimeEnvironmentCollector:
    """Tests for RuntimeEnvironmentCollector class."""

    def test_init_default_workspace(self):
        """默认使用当前工作目录。"""
        collector = RuntimeEnvironmentCollector()
        assert collector.workspace_path == Path.cwd()

    def test_init_custom_workspace(self):
        """支持自定义workspace路径。"""
        custom_path = Path("/tmp/test")
        collector = RuntimeEnvironmentCollector(custom_path)
        assert collector.workspace_path == custom_path

    def test_collect_system_info(self):
        """收集系统信息。"""
        collector = RuntimeEnvironmentCollector()
        with patch.object(collector, "_get_database_info", return_value={}):
            result = collector.collect()

        assert "system" in result
        system = result["system"]
        assert "os_type" in system
        assert "os_release" in system
        assert "arch" in system
        assert "timezone" in system

    def test_collect_runtime_info(self):
        """收集Python运行时信息。"""
        collector = RuntimeEnvironmentCollector()
        with patch.object(collector, "_get_database_info", return_value={}):
            result = collector.collect()

        assert "runtime" in result
        runtime = result["runtime"]
        assert "python_version" in runtime
        assert "python_executable" in runtime
        assert runtime["python_executable"] == sys.executable
        assert "env_type" in runtime
        assert runtime["env_type"] in ["system", "venv", "conda"]

    def test_collect_resources_with_psutil(self):
        """有psutil时收集资源信息。"""
        collector = RuntimeEnvironmentCollector()
        with patch.object(collector, "_get_database_info", return_value={}):
            result = collector.collect()

        assert "resources" in result
        resources = result["resources"]
        assert "available" in resources

        if resources["available"]:
            assert "cpu_count" in resources
            assert "memory_total_gb" in resources

    def test_collect_without_psutil(self):
        """无psutil或psutil异常时降级处理。"""
        with patch.dict("sys.modules", {"psutil": None}):
            collector = RuntimeEnvironmentCollector()
            result = collector._get_resource_info()

            assert result == {}

    def test_collect_resource_info_psutil_runtime_error(self):
        """psutil运行时异常不应阻断环境信息收集。"""
        collector = RuntimeEnvironmentCollector()
        with patch.dict("sys.modules", {"psutil": object()}):
            result = collector._get_resource_info()
        assert result == {}

    def test_collect_with_models(self):
        """收集 planner 模型上下文窗口信息。"""
        collector = RuntimeEnvironmentCollector()

        llm_configs = {
            "planner": {"model": "deepseek-v3.2", "context_windows": 64000},
            "deepseek": {"model": "deepseek-v3.2"},  # 重复模型应去重
            "other": {"model": "unknown-model"},
        }

        result = collector.collect_with_models(llm_configs)

        assert "planner_model" in result
        assert result["planner_model"]["model"] == "deepseek-v3.2"
        assert result["planner_model"]["context_window"] == 64000  # 用户配置优先

    def test_get_database_url_from_database_config(self):
        """从DATABASE配置获取URL。"""
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: {
            "DATABASE": {"config": {"path": "mysql://user:pass@host/db"}},
        }.get(key, default)

        collector = RuntimeEnvironmentCollector()
        url = collector._get_database_url(mock_config)

        assert url == "mysql://user:pass@host/db"

    def test_database_engine_missing_skip(self):
        """拿不到 DATABASE.engine 时应跳过数据库信息展示。"""
        mock_config = MagicMock()
        mock_config.get.return_value = {}
        collector = RuntimeEnvironmentCollector(agent_config_manager=mock_config)
        with (
            patch.object(collector, "_get_database_url", return_value="/tmp/test.sqlite"),
            patch.object(collector, "_check_db_readable", return_value=True),
        ):
            info = collector._get_database_info()
        assert info == {}

    def test_database_engine_prefer_config(self):
        """DATABASE.engine 存在时应优先使用，不从 URL/路径推断。"""
        mock_config = MagicMock()
        mock_config.get.side_effect = lambda key, default=None: (
            {"engine": "mysql", "db_id": "x"} if key == "DATABASE" else default
        )
        collector = RuntimeEnvironmentCollector(agent_config_manager=mock_config)
        with (
            patch.object(collector, "_get_database_url", return_value="/tmp/test.sqlite"),
            patch.object(collector, "_check_db_readable", return_value=True),
        ):
            info = collector._get_database_info()
        assert info["engine"] == "mysql"

    def test_check_db_readable_sqlite_file_path(self, tmp_path):
        """sqlite 文件路径应只读打开且不会创建新文件。"""
        collector = RuntimeEnvironmentCollector()

        # 1) 不存在的路径：必须返回 False（避免创建文件）
        assert collector._check_db_readable("/tmp/__not_exist_for_test__.sqlite") is False

        # 2) 存在且是有效 sqlite 文件：应走只读连接
        db_path = tmp_path / "test.sqlite"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("SELECT 1")

        assert collector._check_db_readable(str(db_path)) is True

    def test_format_with_zero_cpu_usage(self):
        """CPU 使用率为 0.0 时也应展示。"""
        env_data = {
            "system": {"os_type": "Linux", "os_release": "5.15", "arch": "x86_64"},
            "runtime": {"python_version": "3.11", "env_type": "system"},
            "resources": {
                "available": True,
                "cpu_count": 8,
                "cpu_usage_percent": 0.0,
                "memory_total_gb": 16.0,
                "memory_used_gb": 8.0,
                "memory_percent": 50.0,
            },
        }

        result = format_runtime_environment(env_data)

        assert "CPU: 0.0% (8 cores)" in result

    # NOTE: runtime_env 目前只展示 DATABASE.config.path，不再支持 DATASOURCE 入口。


class TestFormatRuntimeEnvironment:
    """Tests for formatting functions."""

    def test_format_basic_structure(self):
        """基本格式化结构。"""
        env_data = {
            "system": {
                "os_type": "Linux",
                "os_release": "5.15.0",
                "arch": "x86_64",
                "timezone": "UTC",
            },
            "runtime": {
                "python_version": "3.11.0",
                "env_type": "venv",
                "python_executable": "/opt/venv/bin/python",
            },
            "resources": {"available": False},
        }

        result = format_runtime_environment(env_data)

        assert "OS: Linux 5.15.0 (x86_64)" in result
        assert "Timezone: UTC" in result
        assert "Python: 3.11.0 (venv)" in result
        assert "Python interpreter: /opt/venv/bin/python" in result

    def test_format_with_resources(self):
        """包含资源信息的格式化。"""
        env_data = {
            "system": {"os_type": "Linux", "os_release": "5.15", "arch": "x86_64"},
            "runtime": {"python_version": "3.11", "env_type": "system"},
            "resources": {
                "available": True,
                "cpu_count": 8,
                "cpu_usage_percent": 10.5,
                "memory_total_gb": 16.0,
                "memory_used_gb": 8.0,
                "memory_percent": 50.0,
            },
        }

        result = format_runtime_environment(env_data)

        assert "CPU: 10.5% (8 cores)" in result
        assert "Memory: 8.0GB / 16.0GB (50.0%)" in result

    def test_format_with_database(self):
        """包含数据库信息的格式化。"""
        env_data = {
            "system": {"os_type": "Linux", "os_release": "5.15", "arch": "x86_64"},
            "runtime": {"python_version": "3.11", "env_type": "system"},
            "resources": {"available": False},
            "database": {"db_id": "testdb", "engine": "mysql", "readable": True},
        }

        result = format_runtime_environment(env_data)

        assert "Database (mysql): testdb (readable)" in result

    def test_format_with_planner_context_window(self):
        """包含 planner 模型上下文窗口的格式化。"""
        env_data = {
            "system": {"os_type": "Linux", "os_release": "5.15", "arch": "x86_64"},
            "runtime": {"python_version": "3.11", "env_type": "system"},
            "resources": {"available": False},
            "planner_model": {"model": "deepseek-v3", "context_window": 131072},
        }

        result = format_runtime_environment(env_data)

        assert "Planner context window: 131k (deepseek-v3)" in result

    def test_format_section_with_title(self):
        """带标题的完整段落格式化。"""
        env_data = {
            "system": {"os_type": "Linux", "os_release": "5.15", "arch": "x86_64"},
            "runtime": {"python_version": "3.11", "env_type": "system"},
            "resources": {"available": False},
        }

        result = format_runtime_environment_section(env_data)

        assert result.startswith("## Runtime Environment\n")
        assert "OS: Linux" in result

    def test_format_empty_data(self):
        """空数据返回空字符串。"""
        result = format_runtime_environment({})
        # 至少应该有OS行（使用默认值）
        assert "OS: Unknown" in result
