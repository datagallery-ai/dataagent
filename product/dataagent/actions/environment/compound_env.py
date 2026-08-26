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
from dataagent.actions.environment.env import Env


class CompoundEnv(Env):
    """
    Combines multiple Env instances into a single unified environment.

    The CompoundEnv merges all tools from the component environments,
    and delegates init/close operations to all sub-environments.

    Example usage:
        >>> class MathEnv(Env):
        ...     @Env.tool
        ...     def add(self, a, b):
        ...         return a + b
        ...
        >>> class StringEnv(Env):
        ...     @Env.tool
        ...     def concat(self, a, b):
        ...         return a + b
        ...
        >>> math_env = MathEnv()
        >>> string_env = StringEnv()
        >>> compound = CompoundEnv([math_env, string_env])
        >>> compound.tools['add'](5, 3)
        8
        >>> compound.tools['concat']('Hello', ' World')
        'Hello World'

    Notes:
        - Tools from later environments override tools with the same name
          from earlier environments
        - Calling init() on CompoundEnv initializes all sub-environments
        - Calling close() on CompoundEnv closes all sub-environments
    """

    def __init__(self, envs: list[Env]):
        """
        Initialize CompoundEnv with a list of Env instances.

        Args:
            envs: List of Env instances to combine
        """
        # Set envs BEFORE calling super().__init__() because _register_tools() needs it
        self.envs = envs
        super().__init__()

    def __repr__(self) -> str:
        """String representation showing number of environments and tools"""
        return f"CompoundEnv(envs={len(self.envs)}, tools={len(self.tools)})"

    def init(self):
        """Initialize all component environments"""
        for env in self.envs:
            env.init()

    def close(self):
        """Close all component environments"""
        for env in self.envs:
            env.close()

    def get_description(self) -> str:
        """Combine all descriptions"""
        return "\n".join([env.get_description() for env in self.envs])

    def _register_tools(self):
        """
        Override parent's _register_tools to merge tools from all sub-environments.

        Tools from later environments will override tools with the same name
        from earlier environments.
        """
        super()._register_tools()

        for env in self.envs:
            env_tools = env.get_tools()
            self.tools.update(env_tools)
