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
from collections.abc import Callable


class Env:
    def __init__(self):
        self.tools: dict[str, Callable] = {}
        self.init()
        self._register_tools()

    def __del__(self):
        self.close()

    @staticmethod
    def tool(func: Callable) -> Callable:
        """Decorator to mark a method as a tool"""
        func._is_tool = True
        return func

    def init(self):
        """Initialize the environment, override in subclass"""
        pass

    def close(self):
        """Close the environment, override in subclass"""
        pass

    def get_tools(self) -> dict[str, Callable]:
        """Return the tools bound the the environment"""
        return self.tools

    def get_description(self) -> str:
        """Return the description of the environment"""
        return ""

    def _register_tools(self):
        """Register all methods marked with the @tool decorator"""
        for name in dir(self):
            attr = getattr(self, name)
            if callable(attr) and hasattr(attr, "_is_tool"):
                self.tools[name] = attr
