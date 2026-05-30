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
"""Jinja2 提示词载体与包内 md 加载。

:class:`PromptTemplate` 提供 ``from_string`` / ``from_file`` / ``from_package_relative`` / ``with_partials``。
其中 ``from_package_relative`` 按 **已安装 ``dataagent`` 包根** 下的相对路径读 ``*.md``（无后缀），
内部复用 ``from_file``，**无进程级缓存**；失败时统一 ``ValueError``（与历史 ``get_prompt`` 契约对齐）。
"""

from __future__ import annotations

__all__ = ["PROMPT_MD_PREFIX", "PromptTemplate"]

from collections.abc import Mapping
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError, UndefinedError  # type: ignore[import-not-found]

from dataagent.utils.runtime_paths import dataagent_package_path

#: 内置 ``*.md`` 提示词所在目录相对 ``dataagent`` 包根的路径前缀（无首尾 ``/``）。
PROMPT_MD_PREFIX = "core/managers/prompt_manager/templates"


class PromptTemplate:
    """Jinja2 提示词模板：持有正文 ``content`` 与可选局部模板，按需编译与渲染。"""

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
        """从字符串创建 ``PromptTemplate``。"""
        return cls(content)

    @classmethod
    def from_file(cls, file_path: str, *, encoding: str = "utf-8") -> PromptTemplate:
        """从文件读取正文创建 ``PromptTemplate``。"""
        content = Path(file_path).read_text(encoding=encoding)
        return cls(content)

    @classmethod
    def from_package_relative(cls, path: str) -> PromptTemplate:
        """从 ``dataagent`` 包根下的相对路径读取 ``.md``，创建 ``PromptTemplate``。

        Args:
            path: POSIX、无 ``.md`` 后缀，例如 ``f"{PROMPT_MD_PREFIX}/planner/system"``。

        Raises:
            ValueError: path 非法、含 ``..``、或文件不可读（包装 ``FileNotFoundError`` / ``OSError``）。
        """
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise ValueError(f"Invalid prompt path: {path!r}")
        if ".." in parts:
            raise ValueError(f"Invalid prompt path (no '..' segments): {path!r}")

        md = dataagent_package_path(*parts).with_suffix(".md")
        try:
            return cls.from_file(str(md))
        except FileNotFoundError as e:
            raise ValueError(f"Prompt template not found: {path!r} (looked at {md})") from e
        except OSError as e:
            raise ValueError(f"Failed to read prompt template {path!r}: {e}") from e

    def with_partials(self, **partials: PromptTemplate | None) -> PromptTemplate:
        """返回带局部模板变量的新实例，渲染时 partial 会先使用同一上下文渲染。"""
        valid_partials = {name: partial for name, partial in partials.items() if partial is not None}
        if not valid_partials:
            return self
        return PromptTemplate(self.content, {**self._partials, **valid_partials})

    def apply_prompt_template(self, **kwargs) -> str:
        """使用 Jinja2 渲染 ``content``。

        Args:
            **kwargs: 模板变量

        Returns:
            str: 渲染后的字符串

        Raises:
            ValueError: 缺少变量、语法错误或渲染失败
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
