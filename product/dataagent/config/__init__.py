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
"""DataAgent YAML configuration.

Create an isolated :class:`ConfigManager` per Agent or script::

    cm = ConfigManager()
    cm.reload("agent.yaml", default_config_path="flex_default_configs.yaml")

Agent runtime must use ``agent.config``, ``runtime.config_manager``, or
``ToolExecutionContext`` — not module-level configuration singletons.
"""

__all__ = [
    "ConfigManager",
    "create_config_manager",
    "build_prompt",
]

from dataagent.config.config_manager import ConfigManager, build_prompt


def create_config_manager(config_path=None):
    """Create a new isolated :class:`ConfigManager` instance."""
    return ConfigManager(config_path)
