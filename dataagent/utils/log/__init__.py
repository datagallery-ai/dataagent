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
"""Public logging package entrypoint.

Keep imports stable at ``dataagent.utils.log`` while the concrete implementation
lives in ``dataagent.utils.log.dataagent_logger`` to avoid colliding with the compiled
``logger`` extension module that may also exist in this package directory.
"""

__all__ = [
    # 核心类
    "DataAgentLogger",
    "LoggerConfig",
    # 全局实例和函数
    "init_logger",
    "get_logger",
    "reconfigure",
    "setup_subprocess_logging",
]

from dataagent.utils.env_utils import get_env

from .dataagent_logger import (
    DataAgentLogger,
    LoggerConfig,
    get_logger,
    init_logger,
    reconfigure,
    setup_subprocess_logging,
)

# 从环境变量或默认配置初始化日志
_log_console_level = get_env("DATAAGENT_LOG_LEVEL", default="INFO")
_log_file_level = get_env("DATAAGENT_LOG_FILE_LEVEL", default="TRACE")
_log_file = get_env("DATAAGENT_LOG_FILE")  # 未设置时自动落到默认会话日志路径
_log_console_raw = get_env("DATAAGENT_LOG_CONSOLE", default="true")
_log_console = (_log_console_raw or "true").lower() == "true"

# 初始化全局日志 - 默认控制台 + 会话日志文件
init_logger(
    LoggerConfig(
        console_level=_log_console_level or "INFO",
        file_level=_log_file_level or "TRACE",
        file_path=_log_file,
        console=_log_console,
        rotation="100 MB",
        retention="7 days",
        file_path_explicit=_log_file is not None,
    )
)

logger = get_logger()
