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
        (["--tier", "ut", "--scope", "dataagent"], ["python-prepare", "dataagent-ut"]),
        (["--tier", "ut", "--scope", "dataagent", "--no-sync"], ["dataagent-ut"]),
        (["--tier", "ut", "--scope", "web"], ["web"]),
        (
            ["--tier", "ut"],
            ["python-prepare", "dataagent-ut", "api", "web", "tui", "deploy", "yaml"],
        ),
        (
            ["--tier", "test", "--scope", "dataagent"],
            ["python-prepare", "dataagent-ut", "tui-build", "dataagent-st"],
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
        if name == "python-prepare":
            assert command == ("uv", "sync")
        if name.startswith("dataagent"):
            assert "--no-sync" in command


def test_preparation_failure_stops_before_running_suites(runner, monkeypatch):
    calls = []

    def run(name, command):
        calls.append(name)
        return runner.SuiteResult(name, command, 1, 0)

    monkeypatch.setattr(runner, "_run", run)
    assert runner.main(["--scope", "dataagent"]) == 1
    assert calls == ["python-prepare"]


def test_listing_commands_does_not_prepare_dependencies(runner, monkeypatch):
    monkeypatch.setattr(
        runner, "_run", lambda *args: pytest.fail("Listing must not execute commands")
    )
    assert runner.main(["--scope", "dataagent", "--list"]) == 0


def test_api_scope_rejects_node_test_options(runner):
    with pytest.raises(SystemExit, match="Use PYTEST_ARGS for Python tests"):
        runner.main(["--scope", "api", "--node-test-args=--maxWorkers 2", "--list"])
