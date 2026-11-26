#!/usr/bin/env python3
"""Run the refactor product tests from the repository root.

Tiers:
- ``ut``: unit tests (dataagent except the product TUI flows, plus API, web, and TUI).
- ``st``: system tests that start a Python control plane, then attach the real TUI.
- ``test``: ut + st.

CI example::

    make test UV_RUN_OPTS=--no-sync
    uv run --no-project python scripts/run_product_tests.py --tier ut --scope dataagent

``--no-sync`` skips Python dependency preparation. Node suites expect ``npm install``.

``--pytest-args`` and ``--node-test-args`` are shell strings. They are split with
``shlex.split`` and appended to the pytest or Node command. An empty string adds
no argument. Make passes them as ``PYTEST_ARGS`` and ``NODE_TEST_ARGS``.

``-n`` / ``--numprocesses`` (including ``-n4`` and ``--numprocesses=4``) may reach
dataagent unit tests. System tests never receive them. ``--tier st`` exits before
starting a TUI when those flags are present. Other filters, such as ``-k``, stay
on both tiers. ``-n`` is never added unless the caller passed it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts" / "product"

# Component order. dataagent is split into unit files and system files.
COMPONENTS = ("dataagent", "api", "web", "tui", "deploy")
NODE_SUITES = ("web", "tui", "deploy", "yaml")
TIERS = ("ut", "st", "test")
ST_FILES = (
    ROOT / "tests" / "test_a2a_e2e.py",
    ROOT / "tests" / "test_product_e2e.py",
    ROOT / "tests" / "test_home_startup.py",
    ROOT / "tests" / "test_tui_cursor.py",
)


@dataclass(frozen=True)
class SuiteResult:
    name: str
    command: tuple[str, ...]
    exit_code: int
    seconds: float


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    selected = _selected_suites(args.scope, args.tier)
    pytest_args = _split_extra(args.pytest_args, "PYTEST_ARGS")
    node_args = _split_extra(args.node_test_args, "NODE_TEST_ARGS")
    if node_args and not any(name in NODE_SUITES for name in selected):
        raise SystemExit(
            "NODE_TEST_ARGS requires a Web, TUI or deployment test suite. Use PYTEST_ARGS for Python tests."
        )
    serial_pytest_args, parallel_args = _partition_xdist(pytest_args)
    if args.tier == "st" and parallel_args:
        raise SystemExit(_st_parallel_message(parallel_args))
    commands = {
        name: _command(
            name,
            pytest_args=serial_pytest_args if name == "dataagent-st" else pytest_args,
            node_args=node_args,
        )
        for name in selected
    }

    if args.list or args.dry_run:
        for name in selected:
            print(f"{name}: {_format_command(commands[name])}")
        return 0

    _require_tools(selected)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    results: list[SuiteResult] = []
    if not args.no_sync and any(name.startswith("dataagent") or name == "api" for name in selected):
        prepared = _run("python-prepare", ("uv", "sync"))
        if prepared.exit_code != 0:
            _write_summary([prepared])
            _print_summary([prepared])
            return prepared.exit_code
    for name in selected:
        if name == "dataagent-st":
            built = _run("tui-build", ("npm", "run", "build:tui"))
            results.append(built)
            if built.exit_code != 0:
                if args.fail_fast:
                    break
                continue
        results.append(_run(name, commands[name]))
        if args.fail_fast and results[-1].exit_code != 0:
            break

    _write_summary(results)
    _print_summary(results)
    return 0 if all(item.exit_code == 0 for item in results) else 1


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run dataagent, API, web, and TUI tests.")
    parser.add_argument(
        "--scope",
        default="all",
        help=(
            "Comma-separated components: dataagent, api, web, tui, deploy, all. "
            "Default all also runs deployment and YAML checks."
        ),
    )
    parser.add_argument(
        "--tier",
        default="test",
        choices=TIERS,
        help="ut, st, or test (ut+st). Default is test.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Skip Python dependency preparation. Use an already prepared environment.",
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="Stop after the first failing suite."
    )
    parser.add_argument("--list", action="store_true", help="Print the selected commands and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Same as --list.")
    parser.add_argument(
        "--pytest-args",
        default="",
        help="Extra pytest arguments for dataagent ut and st, split with shlex. Empty adds nothing.",
    )
    parser.add_argument(
        "--node-test-args",
        default="",
        help="Extra arguments for vitest and node --test suites, split with shlex. Empty adds nothing.",
    )
    return parser.parse_args(list(argv))


def _selected_suites(scope: str, tier: str) -> tuple[str, ...]:
    requested = [item.strip() for item in scope.split(",") if item.strip()]
    if not requested:
        raise SystemExit("scope is empty")
    unknown = [item for item in requested if item != "all" and item not in COMPONENTS]
    if unknown:
        names = ", ".join(unknown)
        allowed = ", ".join((*COMPONENTS, "all"))
        raise SystemExit(f"unknown scope: {names}. Choose from: {allowed}")
    chosen = set(COMPONENTS if "all" in requested else requested)

    names: list[str] = []
    if tier in {"ut", "test"} and "dataagent" in chosen:
        names.append("dataagent-ut")
    if tier in {"ut", "test"}:
        names.extend(name for name in ("api", "web", "tui", "deploy") if name in chosen)
        if "all" in requested:
            names.append("yaml")
    if tier in {"st", "test"} and "dataagent" in chosen:
        names.append("dataagent-st")
    if not names:
        raise SystemExit(f"scope {scope} has no {tier} tests")
    return tuple(names)


def _split_extra(raw: str, label: str) -> tuple[str, ...]:
    text = raw.strip()
    if not text:
        return ()
    try:
        return tuple(shlex.split(text))
    except ValueError as exc:
        raise SystemExit(f"invalid {label}: {exc}") from exc


# Pytest options that consume the next token, so a following -n is a value, not xdist.
_TAKES_VALUE = frozenset(
    {
        "-k",
        "--keyword",
        "-m",
        "--markers",
        "-p",
        "-o",
        "--override-ini",
        "-c",
        "--config-file",
        "--tb",
        "--maxfail",
        "--durations",
        "--durations-min",
        "--dist",
        "--tx",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--junitxml",
        "--rootdir",
        "--confcutdir",
        "--basetemp",
        "--log-level",
        "--log-file",
        "--log-file-level",
        "--log-format",
        "--log-date-format",
        "-W",
        "--pythonwarnings",
        "--pdbcls",
        "--import-mode",
        "--assert",
        "--capture",
    }
)


def _is_attached_xdist(token: str) -> bool:
    if token.startswith("--numprocesses="):
        return True
    return token.startswith("-n") and not token.startswith("--") and token != "-n"


def _partition_xdist(args: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (args without xdist process flags, those flags).

    ``-n 2``, ``-n4``, ``-nauto``, ``-n=4``, ``--numprocesses 4`` and
    ``--numprocesses=4`` are the process flags. ``-k`` expressions are kept.
    """
    kept: list[str] = []
    parallel: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-n", "--numprocesses"}:
            parallel.append(token)
            if index + 1 < len(args):
                parallel.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if _is_attached_xdist(token):
            parallel.append(token)
            index += 1
            continue
        kept.append(token)
        if token in _TAKES_VALUE and "=" not in token and index + 1 < len(args):
            index += 1
            kept.append(args[index])
        index += 1
    return tuple(kept), tuple(parallel)


def _st_parallel_message(parallel: tuple[str, ...]) -> str:
    shown = " ".join(parallel)
    return (
        "系统测试不能并发："
        f"PYTEST_ARGS 含 {shown}。"
        "这些 -n / --numprocesses 参数会并行启动多套 TUI，端口会在 bind 之后、服务 listen 之前被抢走。"
        "请去掉这些参数。"
        "make test 或 make ut 仍可以把 -n 交给单元测试；同一次命令里的系统测试会去掉它们并串行运行。"
    )


def _command(
    name: str,
    *,
    pytest_args: tuple[str, ...] = (),
    node_args: tuple[str, ...] = (),
) -> tuple[str, ...]:
    uv = ["uv", "run", "--no-sync"]
    if name == "dataagent-ut":
        tests = ROOT / "tests"
        command = [
            *uv,
            "--project",
            str(ROOT),
            "pytest",
            str(tests),
            "-q",
            "-m",
            "not live",
            f"--junitxml={ARTIFACTS / 'dataagent-ut-junit.xml'}",
        ]
        for path in (*ST_FILES, ROOT / "tests" / "test_product_api.py"):
            command.extend(["--ignore", str(path)])
        command.extend(pytest_args)
        return tuple(command)
    if name == "dataagent-st":
        command = [
            *uv,
            "--project",
            str(ROOT),
            "pytest",
            *(str(path) for path in ST_FILES),
            "-q",
            "-m",
            "not live",
            f"--junitxml={ARTIFACTS / 'dataagent-st-junit.xml'}",
        ]
        command.extend(pytest_args)
        return tuple(command)
    if name == "api":
        command = [
            *uv,
            "--project",
            str(ROOT),
            "pytest",
            str(ROOT / "tests" / "test_product_api.py"),
            "-q",
        ]
        command.extend(pytest_args)
        return tuple(command)
    if name in NODE_SUITES:
        command = ("npm", "run", f"test:{name}")
        return (*command, "--", *node_args) if node_args else command
    raise SystemExit(f"unknown suite: {name}")


def _require_tools(selected: Sequence[str]) -> None:
    needs_uv = any(name.startswith("dataagent") or name == "api" for name in selected)
    needs_npm = any(name in (*NODE_SUITES, "dataagent-st") for name in selected)
    if needs_uv and shutil.which("uv") is None:
        raise SystemExit("uv is not on PATH")
    if needs_npm and shutil.which("npm") is None:
        raise SystemExit("npm is not on PATH")


def _run(name: str, command: tuple[str, ...]) -> SuiteResult:
    print(f"\n==> {name}\n$ {_format_command(command)}", flush=True)
    started = time.monotonic()
    env = dict(os.environ)
    # ps -o command= truncates to the terminal width. DataAgent checks the full command line.
    env.setdefault("COLUMNS", "1000")
    # Ink skips the real terminal when CI is set, so TUI system tests never see Enter.
    if name == "dataagent-st":
        env.pop("CI", None)
    completed = subprocess.run(command, cwd=ROOT, env=env)
    return SuiteResult(name, command, completed.returncode, time.monotonic() - started)


def _write_summary(results: Sequence[SuiteResult]) -> None:
    payload = {
        "ok": all(item.exit_code == 0 for item in results),
        "suites": [
            {
                "name": item.name,
                "exit_code": item.exit_code,
                "seconds": round(item.seconds, 3),
                "command": list(item.command),
            }
            for item in results
        ],
    }
    (ARTIFACTS / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _print_summary(results: Sequence[SuiteResult]) -> None:
    print("\n==> summary")
    for item in results:
        status = "passed" if item.exit_code == 0 else "failed"
        print(f"{item.name:<10} {status:<7} exit={item.exit_code} {item.seconds:.1f}s")


def _format_command(command: Sequence[str]) -> str:
    return shlex.join(command)


if __name__ == "__main__":
    raise SystemExit(main())
