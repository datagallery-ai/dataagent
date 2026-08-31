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
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timezone
from pathlib import Path

SCOPE_TARGETS: dict[str, tuple[str, ...]] = {
    "ut": ("tests/ut",),
    "st": ("tests/st",),
    "all": ("tests/ut", "tests/st"),
}
VALID_SCOPES = ("ut", "st", "all")


def main(argv: list[str] | None = None) -> int:
    """运行 ut/st 测试并可选生成覆盖率与 junit 产物。

    用法::

        python tests/run_test.py --scope ut|st|all [--cov] [--junit|--no-junit] [-- -k foo]

    说明：
    - 默认 scope 读取环境变量 ``TEST_SCOPE``，缺省为 ``ut``；仅支持 ``ut`` / ``st`` / ``all``。
    - ``all`` = ``tests/ut`` + ``tests/st``，不包含 integration / e2e / benchmark。
    - 仅显式传入 ``--cov`` 时生成覆盖率报告；junit 默认开启（可用 ``--no-junit`` 关闭）。
    - 产物写入 ``tests/artifacts/<scope>/``（跑前清空该目录）。
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root = _repo_root()
    scope = args.scope
    targets = list(SCOPE_TARGETS[scope])
    artifacts_dir = root / "tests" / "artifacts" / scope

    _prepare_artifacts_dir(artifacts_dir)

    env = dict(os.environ)
    coverage_data = artifacts_dir / ".coverage"
    env["COVERAGE_FILE"] = str(coverage_data)

    started = datetime.now(UTC)
    proc = _run_pytest(
        root=root,
        targets=targets,
        artifacts_dir=artifacts_dir,
        cov=args.cov,
        junit=args.junit,
        pytest_args=args.pytest_args,
        env=env,
    )
    exit_code = proc.returncode

    if args.cov and exit_code == 0:
        exit_code = _generate_coverage_reports(root, artifacts_dir, env=env)

    _write_meta(
        artifacts_dir=artifacts_dir,
        scope=scope,
        targets=targets,
        cov=args.cov,
        junit=args.junit,
        pytest_args=_normalize_pytest_extra_args(args.pytest_args),
        exit_code=exit_code,
        started=started,
        coverage_data=coverage_data if args.cov else None,
    )
    return exit_code


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _rm_rf(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except FileNotFoundError:
        return


def _prepare_artifacts_dir(artifacts_dir: Path) -> None:
    _rm_rf(artifacts_dir)
    artifacts_dir.mkdir(parents=True, exist_ok=True)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    default_scope = os.environ.get("TEST_SCOPE", "ut")
    parser = argparse.ArgumentParser(
        description="运行 ut/st 测试（不含 live 用例），可选生成覆盖率与 junit 产物。",
    )
    parser.add_argument(
        "--scope",
        default=default_scope,
        choices=VALID_SCOPES,
        help="测试范围：ut / st / all。默认读取环境变量 TEST_SCOPE，缺省为 ut。",
    )
    parser.add_argument(
        "--cov",
        action="store_true",
        help="生成覆盖率报告（coverage.xml / htmlcov），默认关闭。",
    )
    parser.add_argument(
        "--junit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="写入 junit.xml（默认开启；可用 --no-junit 关闭）。",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="额外透传给 pytest 的参数（放在 -- 之后，例如：-- -k test_x -vv）。",
    )
    return parser.parse_args(list(argv))


def _normalize_pytest_extra_args(pytest_args: Sequence[str]) -> list[str]:
    if pytest_args and pytest_args[0] == "--":
        return list(pytest_args[1:])
    return list(pytest_args)


def _run_pytest(
    *,
    root: Path,
    targets: Sequence[str],
    artifacts_dir: Path,
    cov: bool,
    junit: bool,
    pytest_args: Sequence[str],
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    cmd = [sys.executable, "-m", "pytest", *targets]
    if junit:
        cmd.append(f"--junitxml={artifacts_dir / 'junit.xml'}")
    if cov:
        # 先只生成 .coverage；随后 touch 全量源码再出 xml/html
        cmd.extend(["--cov=dataagent", "--cov-report="])
    cmd.extend(_normalize_pytest_extra_args(pytest_args))
    return subprocess.run(cmd, cwd=str(root), env=env)


def _touch_all_source_files(root: Path, *, env: dict[str, str]) -> int:
    """把 dataagent/**/*.py 写入 coverage 数据，使未执行文件以 0% 出现在报告中。"""
    code = r"""
from __future__ import annotations
import os
from pathlib import Path

from coverage import Coverage

root = Path(os.environ["DATAAGENT_REPO_ROOT"]).resolve()
data_file = os.environ.get("COVERAGE_FILE") or str(root / ".coverage")
cov = Coverage(data_file=data_file)
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
    run_env = dict(env)
    run_env["DATAAGENT_REPO_ROOT"] = str(root)
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(root), env=run_env)
    return proc.returncode


def _generate_coverage_reports(root: Path, artifacts_dir: Path, *, env: dict[str, str]) -> int:
    if _touch_all_source_files(root, env=env) != 0:
        return 1

    coverage_cmd = [sys.executable, "-m", "coverage"]
    subprocess.run([*coverage_cmd, "report", "-m"], cwd=str(root), env=env, check=False)
    subprocess.run(
        [*coverage_cmd, "xml", "-o", str(artifacts_dir / "coverage.xml")],
        cwd=str(root),
        env=env,
        check=False,
    )
    subprocess.run(
        [*coverage_cmd, "html", "-d", str(artifacts_dir / "htmlcov")],
        cwd=str(root),
        env=env,
        check=False,
    )
    return 0


def _write_meta(
    *,
    artifacts_dir: Path,
    scope: str,
    targets: Sequence[str],
    cov: bool,
    junit: bool,
    pytest_args: Sequence[str],
    exit_code: int,
    started: datetime,
    coverage_data: Path | None,
) -> None:
    finished = datetime.now(UTC)
    meta = {
        "scope": scope,
        "targets": list(targets),
        "cov": cov,
        "junit": junit,
        "pytest_args": list(pytest_args),
        "exit_code": exit_code,
        "python": sys.executable,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "artifacts": {
            "dir": str(artifacts_dir),
            "junit": "junit.xml" if junit else None,
            "coverage_xml": "coverage.xml" if cov else None,
            "htmlcov": "htmlcov" if cov else None,
            "coverage_data": str(coverage_data.name) if coverage_data else None,
        },
    }
    (artifacts_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
