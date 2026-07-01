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
import glob
import os
import shutil

from Cython.Build import cythonize  # pyright: ignore[reportMissingImports]
from setuptools import Extension, find_namespace_packages, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.build_py import build_py

SOURCE_DIRS = ["dataagent"]
EXCLUDE_FILES = {
    "dataagent": {
        "__init__.py",
        "icbc_env.py",
        "__main__.py",
        "schemas.py",
        "server.py",
        "chat_model.py",
        "agent.py",
        "loader.py",
        "main.py",
        "memory.py",
        "workflow_openjiuwen.py",
        "sub_agent_entry.py",  # subagent执行入口，强路径匹配，请勿打包
        "start_service.py",
    }
}
EXCLUDE_DIRS = {
    "dataagent": {
        # relative to repo root
        "dataagent/actions/skills",
        "dataagent/actions/tools/mcp_tool",
    }
}
BUILD_TMP_DIR = "build_cython"


def get_parallel() -> int:
    """Set parallelism number"""
    cpu_count = os.cpu_count() or 1
    return max(1, cpu_count // 2)


def should_exclude(source_dir: str, file_path: str) -> bool:
    """Check if a file should be excluded from packaging"""
    base_name = os.path.basename(file_path)
    if base_name in EXCLUDE_FILES.get(source_dir, set()):
        return True

    rel_path = os.path.relpath(file_path, start=os.getcwd()) if os.path.isabs(file_path) else file_path

    rel_posix = _to_posix_path(rel_path)
    exclude_dirs = EXCLUDE_DIRS.get(source_dir, set())
    for exclude_dir in exclude_dirs:
        prefix = _to_posix_path(exclude_dir).rstrip("/") + "/"
        if rel_posix.startswith(prefix):
            return True

    return False


def _to_posix_path(path: str) -> str:
    return os.path.normpath(path).replace(os.path.sep, "/")


class BuildPy(build_py):
    """重写build_py，排除不需要加密的py文件"""

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        for source_dir in SOURCE_DIRS:
            if package.startswith(source_dir):
                return [(pkg, mod, file) for pkg, mod, file in modules if should_exclude(source_dir, file)]
        return modules


class BuildExt(build_ext):
    """Rewrite build_ext module to support parallel compilation"""

    def run(self):
        self.parallel = get_parallel()
        try:
            super().run()
        finally:
            # 构建中断也清理中间目录，避免遗留 .c 文件
            shutil.rmtree(BUILD_TMP_DIR, ignore_errors=True)


def get_ext_modules():
    """Collect Cython extension modules for packaging"""
    extensions = []
    for source_dir in SOURCE_DIRS:
        extensions.extend(
            [
                Extension(name=str(file_name.replace(".py", "").replace(os.path.sep, ".")), sources=[file_name])
                for file_name in glob.glob(os.path.join(source_dir, "**", "*.py"), recursive=True)
                if not should_exclude(source_dir, file_name)
            ]
        )
    return extensions


kwargs = {
    "packages": find_namespace_packages(include=["dataagent*"]),
    "ext_modules": cythonize(
        get_ext_modules(),
        compiler_directives={"language_level": "3", "annotation_typing": False},
        build_dir=BUILD_TMP_DIR,
        nthreads=get_parallel(),
    ),
    "include_package_data": True,
    "cmdclass": {
        "build_py": BuildPy,
        "build_ext": BuildExt,
    },
}

if __name__ == "__main__":
    setup(**kwargs)
