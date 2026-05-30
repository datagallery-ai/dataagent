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
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

# 配置日志
logger = logging.getLogger(__name__)


@asynccontextmanager
async def timeout_context(timeout: float, operation_name: str = "operation"):
    """异步超时上下文管理器"""
    try:
        async with asyncio.timeout(timeout):
            yield
    except TimeoutError:
        logger.warning(f"{operation_name} timed out after {timeout} seconds")
        raise


async def safe_cleanup_tasks(
    cleanup_tasks: list[Callable[[], Awaitable[Any]]], timeout: float = 5.0, operation_name: str = "cleanup"
) -> list[Any | None]:
    """安全执行多个清理任务，即使某些失败也继续执行其他任务"""
    results = []

    for i, task in enumerate(cleanup_tasks):
        try:
            async with timeout_context(timeout, f"{operation_name}_task_{i}"):
                result = await task()
                results.append(result)
        except Exception as e:
            logger.warning(f"Cleanup task {i} failed: {e}")
            results.append(None)

    return results


def safe_log_warning(message: str, exception: Exception | None = None):
    """安全地记录警告信息"""
    if exception:
        logger.warning(f"{message}: {exception}", exc_info=True)
    else:
        logger.warning(message)


def safe_log_error(message: str, exception: Exception | None = None):
    """安全地记录错误信息"""
    if exception:
        logger.error(f"{message}: {exception}", exc_info=True)
    else:
        logger.error(message)
