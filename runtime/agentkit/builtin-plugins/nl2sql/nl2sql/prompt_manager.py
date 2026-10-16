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
"""Jinja2 prompt templates loaded relative to this plugin package.

Port of the legacy ``dataagent`` PromptTemplate;
``from_package_relative`` now resolves against the plugin's ``nl2sql`` package
root instead of the legacy installed ``dataagent`` package.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError, UndefinedError

_PACKAGE_ROOT = Path(__file__).resolve().parent


class PromptTemplate:
    """Jinja2 prompt template: holds ``content`` plus optional partial templates."""

    def __init__(self, content: str, partials: Mapping[str, PromptTemplate] | None = None):
        self.content = content
        self._partials = dict(partials or {})
        self._jinja_env = Environment()
        self._template = None

    def __str__(self) -> str:
        preview = self.content[: min(40, len(self.content))]
        suffix = "..." if len(self.content) > 40 else ""
        return f"PromptTemplate(content={preview!r}{suffix})"

    def __repr__(self) -> str:
        return self.__str__()

    @classmethod
    def from_string(cls, content: str) -> PromptTemplate:
        """Create a ``PromptTemplate`` from a string."""
        return cls(content)

    @classmethod
    def from_file(cls, file_path: str, *, encoding: str = "utf-8") -> PromptTemplate:
        """Create a ``PromptTemplate`` from a file on disk."""
        content = Path(file_path).read_text(encoding=encoding)
        return cls(content)

    @classmethod
    def from_package_relative(cls, path: str) -> PromptTemplate:
        """Read a ``.md`` template relative to the ``nl2sql`` package root.

        Args:
            path: POSIX, without the ``.md`` suffix, e.g. ``"prompts/generator/prompt_system"``.

        Raises:
            ValueError: path is invalid, contains ``..``, or the file is unreadable.
        """
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"Invalid prompt path: {path!r}")
        if ".." in parts:
            raise ValueError(f"Invalid prompt path (no '..' segments): {path!r}")

        md = (_PACKAGE_ROOT.joinpath(*parts)).with_suffix(".md")
        try:
            return cls.from_file(str(md))
        except FileNotFoundError as e:
            raise ValueError(f"Prompt template not found: {path!r} (looked at {md})") from e
        except OSError as e:
            raise ValueError(f"Failed to read prompt template {path!r}: {e}") from e

    def with_partials(self, **partials: PromptTemplate | None) -> PromptTemplate:
        """Return a new instance with partial templates rendered in the same context."""
        valid_partials = {name: partial for name, partial in partials.items() if partial is not None}
        if not valid_partials:
            return self
        return PromptTemplate(self.content, {**self._partials, **valid_partials})

    def apply_prompt_template(self, **kwargs) -> str:
        """Render ``content`` with Jinja2.

        Raises:
            ValueError: a variable is missing, the template is invalid, or rendering fails.
        """
        try:
            if self._template is None:
                self._template = self._jinja_env.from_string(self.content)

            render_kwargs = dict(kwargs)
            for name, partial in self._partials.items():
                render_kwargs[name] = partial.apply_prompt_template(**kwargs)
            return self._template.render(**render_kwargs)

        except UndefinedError as e:
            raise ValueError(f"缺少必要的参数: {str(e)}") from e
        except TemplateSyntaxError as e:
            raise ValueError(f"模板语法错误: {str(e)}") from e
        except Exception as e:
            raise ValueError(f"格式化prompt时出错: {e}") from e
