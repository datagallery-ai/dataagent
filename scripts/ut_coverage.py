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
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


# 运行方式：python scripts/ut_coverage.py
def main(argv: list[str]) -> int:
    """运行单元测试并生成覆盖率报告。

    说明：
    - 默认 scope 从环境变量 `SCOPE` 读取，缺省为 `unittest`。
    - `--scope`:
        - `unittest`: 仅运行脚本内维护的 unittest 风格用例。
        - `all`: 运行 `tests/` 下的用例（忽略 `tests/e2e`）。
    - `--no-uv`: 即使本机安装了 uv，也强制使用当前 Python 解释器运行 `pytest`。
    - 其余 pytest 参数需放在 `--` 之后，会被原样透传给 pytest，例如：`-- -k test_x -vv`。

    Args:
        argv: 命令行参数，通常传入 `sys.argv[1:]`。

    Returns:
        pytest 的退出码；成功（0）时会在 `scripts/coverage/` 生成 XML 与 HTML 报告。
    """
    args = _parse_args(argv)
    root = _repo_root()
    output_dir = root / "scripts" / "coverage"

    _cleanup_coverage_artifacts(root, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    proc = _run_pytest_with_cov(root, scope=args.scope, no_uv=args.no_uv, pytest_args=args.pytest_args)
    if proc.returncode != 0:
        return proc.returncode
    return _generate_coverage_reports(root, output_dir, no_uv=args.no_uv)


def _repo_root() -> Path:
    # scripts/ut_coverage.py -> repo root is parent of scripts/
    return Path(__file__).resolve().parent.parent


def _rm_rf(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return


def _cleanup_coverage_artifacts(root: Path, output_dir: Path) -> None:
    _rm_rf(output_dir / "coverage.xml")
    _rm_rf(output_dir / "htmlcov")
    _rm_rf(root / ".coverage")
    # coverage 在某些模式下会生成并行数据文件（.coverage.*），一并清理避免污染结果
    for p in root.glob(".coverage.*"):
        _rm_rf(p)


def _has_uv() -> bool:
    # 统一禁用 uv：CI/本地环境差异会导致 `uv run` 不可用或行为不一致。
    # 保持该函数仅用于兼容历史代码路径，但永远返回 False，确保不进入 uv 分支。
    return False


def _select_pytest_cmd(no_uv: bool) -> list[str]:
    return [sys.executable, "-m", "pytest"]


def _select_coverage_cmd(no_uv: bool) -> list[str]:
    return [sys.executable, "-m", "coverage"]


def _select_python_cmd(no_uv: bool) -> list[str]:
    """用于执行 `python -c ...` 的命令前缀，确保与 pytest/coverage 处在同一环境。"""
    return [sys.executable]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    default_scope = os.environ.get("SCOPE", "unittest")
    parser = argparse.ArgumentParser(description="UT覆盖率脚本")
    parser.add_argument(
        "--scope",
        default=default_scope,
        choices=["unittest", "all"],
        help="unittest: 仅跑指定目录unittest风格用例；all: 跑 tests目录用例。默认读取环境变量 SCOPE，缺省为 unittest。",
    )
    parser.add_argument(
        "--no-uv",
        action="store_true",
        help="禁用 uv（即使已安装），直接用当前 Python 解释器运行 pytest。",  # 当前 CI 流水线不支持 uv run
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="额外透传给 pytest 的参数（需要放在 -- 之后，例如：-- -k test_x -vv）。",
    )
    return parser.parse_args(argv)


def _select_test_targets(root: Path, scope: str) -> list[str]:
    if scope == "all":
        # 忽略 `tests/e2e`
        return ["tests", "--ignore=tests/e2e"]

    # unittest 模式：在这里维护要收集的测试文件/通配符，方便后续直接追加：
    # - 新增目录：加 "tests/xxx"
    # tests 下 e2e 和 integration 目录为 live 测试，依赖真实API
    # tests 下 ut 和 st 目录的测试用例则不依赖真实API
    unittest_patterns = [
        "tests/ut",
        "tests/st",
    ]
    return unittest_patterns


def _normalize_pytest_extra_args(pytest_args: list[str]) -> list[str]:
    # argparse.REMAINDER 会把 `--` 也原样带上，这里剥离以便透传给 pytest
    if pytest_args and pytest_args[0] == "--":
        return pytest_args[1:]
    return pytest_args


def _run_pytest_with_cov(
    root: Path,
    scope: str,
    no_uv: bool,
    pytest_args: list[str],
) -> subprocess.CompletedProcess[bytes]:
    pytest_cmd = _select_pytest_cmd(no_uv=no_uv)
    test_targets = _select_test_targets(root, scope)
    extra = _normalize_pytest_extra_args(pytest_args)

    # - pytest-cov 默认只会在“被执行/被 import 到”的文件里统计覆盖率；
    # 期望全量报告能覆盖 dataagent/ 下所有目录（即便某些目录在当前 scope 没被运行，也应以 0% 展示）
    # 因此这里先只生成 `.coverage` 数据文件，随后用 coverage API touch 全量源码文件，再统一生成 report/xml/html。
    cov_args = ["--cov=dataagent", "--cov-report="]

    cmd = [*pytest_cmd, *test_targets, *cov_args, *extra]
    return subprocess.run(cmd, cwd=str(root))


def _touch_all_source_files(root: Path, no_uv: bool) -> int:
    """把 `dataagent/**.py` 统一写入 coverage 数据文件，使报告能包含未执行到的目录/文件（0%）。"""
    code = r"""
from __future__ import annotations
import os
from pathlib import Path

from coverage import Coverage

root = Path(os.environ["DATAAGENT_REPO_ROOT"]).resolve()
cov = Coverage(data_file=str(root / ".coverage"))
cov.load()
data = cov.get_data()

measured = list(data.measured_files())
use_abs = bool(measured) and os.path.isabs(measured[0])

def should_skip(p: Path) -> bool:
    parts = set(p.parts)
    if "__pycache__" in parts:
        return True
    if "migrations" in parts:
        return True
    if "tests" in parts:
        return True
    if p.name.startswith("test_"):
        return True
    return False

src_root = root / "dataagent"
for p in src_root.rglob("*.py"):
    if should_skip(p):
        continue
    fp = str(p.resolve()) if use_abs else str(p.relative_to(root))
    data.touch_file(fp)

cov.save()
"""
    env = dict(os.environ)
    env["DATAAGENT_REPO_ROOT"] = str(root)
    env["DATAAGENT_REPO_ROOT"] = str(root)
    proc = subprocess.run([*_select_python_cmd(no_uv), "-c", code], cwd=str(root), env=env)
    return proc.returncode


def _generate_coverage_reports(root: Path, output_dir: Path, no_uv: bool) -> int:
    # 把 dataagent/ 下所有源码文件 touch 进 coverage 数据，确保全量覆盖率报告包含未运行到的目录（例如 dataagent / utils 等）
    # 覆盖率报告生成，相关路径如下：
    # XML（UT覆盖XML）：{output_dir / 'coverage.xml'}"
    # HTML（UT覆盖可视化）：{output_dir / 'htmlcov' / 'index.html'}"
    if _touch_all_source_files(root, no_uv=no_uv) != 0:
        return 1

    coverage_cmd = _select_coverage_cmd(no_uv=no_uv)
    subprocess.run([*coverage_cmd, "report", "-m"], cwd=str(root), check=False)
    subprocess.run(
        [*coverage_cmd, "xml", "-o", str(output_dir / "coverage.xml")],
        cwd=str(root),
        check=False,
    )
    subprocess.run(
        [*coverage_cmd, "html", "-d", str(output_dir / "htmlcov")],
        cwd=str(root),
        check=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
