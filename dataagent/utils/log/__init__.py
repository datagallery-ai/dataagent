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
    "DataAgentLogger",
    "LoggerConfig",
    "attach_session_log_context",
    "build_config_from_env",
    "get_logger",
    "init_logger",
    "reconfigure",
    "reset_session_log_context",
    "set_session_log_context",
    "setup_subprocess_logging",
]

from .dataagent_logger import (
    DataAgentLogger,
    LoggerConfig,
    attach_session_log_context,
    build_config_from_env,
    get_logger,
    init_logger,
    reconfigure,
    reset_session_log_context,
    set_session_log_context,
    setup_subprocess_logging,
)

# One rolling ``main.<pid>.log`` per process under DATAAGENT_LOG_PATH.
init_logger(build_config_from_env())
logger = get_logger()
