"""The product runner prepares Python once, unless the caller skips preparation."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_product_tests.py"
    spec = importlib.util.spec_from_file_location("product_test_runner", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ARTIFACTS", tmp_path)
    monkeypatch.setattr(module, "_require_tools", lambda selected: None)
    monkeypatch.setattr(module, "_write_summary", lambda results: None)
    monkeypatch.setattr(module, "_print_summary", lambda results: None)
    return module


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--tier", "ut", "--scope", "agentkit"], ["python-prepare", "agentkit-ut"]),
        (["--tier", "ut", "--scope", "agentkit", "--no-sync"], ["agentkit-ut"]),
        (["--tier", "ut", "--scope", "web"], ["web"]),
        (
            ["--tier", "test", "--scope", "agentkit"],
            ["python-prepare", "agentkit-ut", "tui-build", "agentkit-st"],
        ),
    ],
)
def test_preparation_is_selected_once_and_skip_is_honored(runner, monkeypatch, args, expected):
    calls = []

    def run(name, command):
        calls.append((name, command))
        return runner.SuiteResult(name, command, 0, 0)

    monkeypatch.setattr(runner, "_run", run)
    assert runner.main(args) == 0
    assert [name for name, _ in calls] == expected
    for name, command in calls:
        if name.startswith("agentkit"):
            assert "--no-sync" in command


def test_preparation_failure_stops_before_running_suites(runner, monkeypatch):
    calls = []

    def run(name, command):
        calls.append(name)
        return runner.SuiteResult(name, command, 1, 0)

    monkeypatch.setattr(runner, "_run", run)
    assert runner.main(["--scope", "agentkit"]) == 1
    assert calls == ["python-prepare"]


def test_listing_commands_does_not_prepare_dependencies(runner, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *args: pytest.fail("Listing must not execute commands")
    )
    assert runner.main(["--scope", "agentkit", "--list"]) == 0


def test_python_preparation_reports_missing_node(runner, monkeypatch):
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        runner, "_run", lambda *args: pytest.fail("Preparation must not start without node")
    )
    with pytest.raises(SystemExit, match="node is not on PATH"):
        runner.main(["--scope", "agentkit"])
