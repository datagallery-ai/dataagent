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
"""工具并发控制

- 并发数：根据 CPU 核数或传入的 max_concurrency 确定
- 支持通过 min(cpu_based, config_based) 取最小值
"""

import asyncio
import time
from typing import Any

from loguru import logger

from dataagent.core.cbb.runtime_env import RuntimeEnvironmentCollector
from dataagent.utils.constants import DEFAULT_CPU_BUFFER, DEFAULT_MAX_CONCURRENCY_CAP, DEFAULT_MIN_CONCURRENCY


class ConcurrencyController:
    """并发控制器

    - 自动检测容器 CPU 限制（支持 cgroup v1/v2/cpuset）
    - 默认并发数：min(16, container_cpu_limit + 4)
    """

    def __init__(self, max_concurrency: int | None = None):
        if max_concurrency is None:
            cpu_count = self._get_container_cpu_limit()
            max_concurrency = max(DEFAULT_MIN_CONCURRENCY, cpu_count + DEFAULT_CPU_BUFFER)

        max_concurrency = min(DEFAULT_MAX_CONCURRENCY_CAP, max(DEFAULT_MIN_CONCURRENCY, max_concurrency))

        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._active_count = 0

    @property
    def max_concurrency(self) -> int:
        """获取最大并发数"""
        return self._max_concurrency

    @staticmethod
    def _get_container_cpu_limit() -> int:
        """获取容器环境的实际 CPU 限制（复用了 RuntimeEnvironmentCollector 的检测逻辑）"""
        try:
            cpu_count = RuntimeEnvironmentCollector.get_cpu_count()
            if cpu_count is not None and cpu_count >= DEFAULT_MIN_CONCURRENCY:
                return int(cpu_count)
        except Exception as e:
            logger.debug(f"[ConcurrencyController] failed to get container CPU limit: {e}")
        return DEFAULT_MIN_CONCURRENCY

    def update_max_concurrency(self, new_limit: int) -> None:
        """更新最大并发数（取当前值和新限制的最小值）

        Args:
            new_limit: YAML 配置的最大并发数
        """
        if new_limit is not None and new_limit > 0:
            self._max_concurrency = min(self._max_concurrency, new_limit)
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
            logger.debug(f"[ConcurrencyController] updated max_concurrency={self._max_concurrency}")

    async def execute(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict,
        coro,
    ) -> Any:
        """在并发控制下执行

        Args:
            tool_call_id: 工具调用 ID
            tool_name: 工具名称
            tool_args: 工具参数
            coro: 协程对象
        """
        logger.debug(
            f"[Concurrency] {tool_name}({tool_call_id[:8]}...) acquiring semaphore "
            f"(active: {self._active_count}, max: {self._max_concurrency})"
        )

        async with self._semaphore:
            self._active_count += 1
            start_time = time.monotonic()

            logger.debug(f"[Concurrency] {tool_name}({tool_call_id[:8]}...) started (active: {self._active_count})")

            try:
                result = await coro
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.debug(
                    f"[Concurrency] {tool_name}({tool_call_id[:8]}...) completed (duration_ms: {duration_ms:.1f})"
                )
                return result
            except Exception as e:
                duration_ms = (time.monotonic() - start_time) * 1000
                logger.warning(
                    f"[Concurrency] {tool_name}({tool_call_id[:8]}...) failed (duration_ms: {duration_ms:.1f}): {e}"
                )
                raise
            finally:
                self._active_count -= 1
