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
"""并发控制单元测试"""

import asyncio

import pytest
from dataagent.actions.tools.concurrency import (
    DEFAULT_CPU_BUFFER,
    DEFAULT_MAX_CONCURRENCY_CAP,
    DEFAULT_MIN_CONCURRENCY,
    ConcurrencyController,
)


class TestConcurrencyControllerConstants:
    """并发控制常量测试"""

    def test_cpu_buffer_constant(self):
        """测试 CPU buffer 常量"""
        assert DEFAULT_CPU_BUFFER == 4

    def test_max_concurrency_cap_constant(self):
        """测试最大并发数上限常量"""
        assert DEFAULT_MAX_CONCURRENCY_CAP == 16

    def test_min_concurrency_constant(self):
        """测试最小并发数常量"""
        assert DEFAULT_MIN_CONCURRENCY == 1


class TestConcurrencyController:
    """并发控制器测试"""

    def test_default_max_concurrency(self):
        """测试默认最大并发数"""
        controller = ConcurrencyController()
        assert controller.max_concurrency >= DEFAULT_MIN_CONCURRENCY

    def test_custom_max_concurrency(self):
        """测试自定义最大并发数"""
        controller = ConcurrencyController(max_concurrency=2)
        assert controller.max_concurrency == 2

    def test_custom_max_concurrency_high_value(self):
        """测试传入较高的并发数值（受上限 DEFAULT_MAX_CONCURRENCY_CAP=16 限制）"""
        controller = ConcurrencyController(max_concurrency=100)
        # 实际值被上限 cap 为 16
        assert controller.max_concurrency == DEFAULT_MAX_CONCURRENCY_CAP

    def test_execute_concurrent_sync(self):
        """测试并发执行（同步方式）"""
        controller = ConcurrencyController(max_concurrency=2)

        async def mock_task(task_id: str, delay: float):
            await asyncio.sleep(delay)
            return f"result_{task_id}"

        async def run_all():
            tasks = [controller.execute(f"task_{i}", f"tool_{i}", {}, mock_task(f"task_{i}", 0.05)) for i in range(4)]
            return await asyncio.gather(*tasks)

        results = asyncio.run(run_all())
        assert len(results) == 4

    def test_execute_with_error_sync(self):
        """测试执行时出错（同步方式）"""

        async def failing_task():
            raise ValueError("Test error")

        async def run_with_error():
            controller = ConcurrencyController()
            return await controller.execute("call_1", "bash", {}, failing_task())

        with pytest.raises(ValueError):
            asyncio.run(run_with_error())

    def test_concurrent_execution_limit_sync(self):
        """测试并发执行限制（同步方式）"""
        controller = ConcurrencyController(max_concurrency=1)

        concurrent_count = 0
        max_concurrent = 0

        async def counting_task():
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.03)
            concurrent_count -= 1
            return "done"

        async def run_all():
            tasks = [controller.execute(f"task_{i}", f"tool_{i}", {}, counting_task()) for i in range(5)]
            await asyncio.gather(*tasks)
            return max_concurrent

        result = asyncio.run(run_all())
        assert result == 1

    def test_single_task_completion_sync(self):
        """测试单个任务正常完成（同步方式）"""

        async def mock_task():
            await asyncio.sleep(0.01)
            return "result"

        async def run():
            controller = ConcurrencyController()
            return await controller.execute("call_1", "read_file", {"path": "/a.txt"}, mock_task())

        result = asyncio.run(run())
        assert result == "result"

    def test_multiple_tasks_order_sync(self):
        """测试多个任务执行顺序不影响结果（同步方式）"""

        async def mock_task(task_id: str):
            await asyncio.sleep(0.02)
            return f"result_{task_id}"

        async def run():
            controller = ConcurrencyController()
            return await asyncio.gather(
                controller.execute("call_1", "tool_1", {}, mock_task("task_1")),
                controller.execute("call_2", "tool_2", {}, mock_task("task_2")),
                controller.execute("call_3", "tool_3", {}, mock_task("task_3")),
            )

        results = asyncio.run(run())
        assert set(results) == {"result_task_1", "result_task_2", "result_task_3"}


class TestUpdateMaxConcurrency:
    """update_max_concurrency 测试"""

    def test_update_max_concurrency_reduces_limit(self):
        """测试更新并发限制（取较小值）"""
        controller = ConcurrencyController(max_concurrency=10)
        assert controller.max_concurrency == 10

        controller.update_max_concurrency(5)
        assert controller.max_concurrency == 5

    def test_update_max_concurrency_ignores_larger_value(self):
        """测试更新时忽略更大的值"""
        controller = ConcurrencyController(max_concurrency=5)
        assert controller.max_concurrency == 5

        controller.update_max_concurrency(10)
        assert controller.max_concurrency == 5

    def test_update_max_concurrency_ignores_none(self):
        """测试更新时忽略 None 值"""
        controller = ConcurrencyController(max_concurrency=8)
        assert controller.max_concurrency == 8

        controller.update_max_concurrency(None)
        assert controller.max_concurrency == 8

    def test_update_max_concurrency_ignores_zero(self):
        """测试更新时忽略 0 值"""
        controller = ConcurrencyController(max_concurrency=8)
        assert controller.max_concurrency == 8

        controller.update_max_concurrency(0)
        assert controller.max_concurrency == 8

    def test_update_max_concurrency_ignores_negative(self):
        """测试更新时忽略负数值"""
        controller = ConcurrencyController(max_concurrency=8)
        assert controller.max_concurrency == 8

        controller.update_max_concurrency(-1)
        assert controller.max_concurrency == 8

    def test_update_max_concurrency_affects_semaphore_sync(self):
        """测试更新并发限制后影响信号量（同步方式）"""
        controller = ConcurrencyController(max_concurrency=10)
        controller.update_max_concurrency(1)

        concurrent_count = 0
        max_concurrent = 0

        async def counting_task():
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.02)
            concurrent_count -= 1
            return "done"

        async def run_all():
            tasks = [controller.execute(f"task_{i}", f"tool_{i}", {}, counting_task()) for i in range(3)]
            await asyncio.gather(*tasks)
            return max_concurrent

        result = asyncio.run(run_all())
        assert result == 1

    def test_multiple_updates_takes_minimum(self):
        """测试多次更新取最小值"""
        controller = ConcurrencyController(max_concurrency=10)

        controller.update_max_concurrency(8)
        assert controller.max_concurrency == 8

        controller.update_max_concurrency(5)
        assert controller.max_concurrency == 5

        controller.update_max_concurrency(7)
        assert controller.max_concurrency == 5
