"""Check the sdist and install the wheel built from it outside the checkout."""

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def installed_package(tmp_path_factory):
    directory = tmp_path_factory.mktemp("installed-package")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
    }
    env["DATAAGENT_HOME"] = str(directory / "home")

    def run(*args):
        return subprocess.run(
            args, cwd=directory, env=env, check=True, capture_output=True, text=True, timeout=180
        )

    run("uv", "build", str(ROOT), "--out-dir", str(directory / "dist"))
    (sdist,) = (directory / "dist").glob("*.tar.gz")
    (wheel,) = (directory / "dist").glob("*.whl")
    run("uv", "venv", "--python", sys.executable, str(directory / "venv"))
    bin_dir = directory / "venv" / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    constraints = directory / "constraints.txt"
    run(
        "uv",
        "export",
        "--project",
        str(ROOT),
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-hashes",
        "--output-file",
        str(constraints),
    )
    run("uv", "pip", "install", "--python", str(python), "-c", str(constraints), str(wheel))
    return run, sdist, wheel, python, bin_dir


def test_sdist_contains_only_python_distribution_files(installed_package):
    _, sdist, _, _, _ = installed_package
    with tarfile.open(sdist) as archive:
        paths = {Path(member.name).parts[1:] for member in archive if member.isfile()}

    metadata = {("LICENSE",), ("README.md",), ("pyproject.toml",), ("PKG-INFO",)}
    top_level_metadata = {path for path in paths if path[0] != "dataagent"}
    assert metadata <= top_level_metadata <= metadata | {(".gitignore",)}
    assert ("dataagent", "bootstrap", "templates", "config.yaml") in paths
    assert ("dataagent", "plugins", "nl2sql", ".plugin.yaml") in paths


def test_wheel_has_one_top_level_package_and_all_builtin_resources(installed_package):
    _, _, wheel, _, _ = installed_package
    with zipfile.ZipFile(wheel) as archive:
        paths = set(archive.namelist())
    assert {
        name.split("/")[0]
        for name in paths
        if "/" in name and not name.split("/")[0].endswith(".dist-info")
    } == {"dataagent"}
    for resource in (ROOT / "dataagent/plugins").rglob("*"):
        if resource.is_file() and "__pycache__" not in resource.parts:
            assert resource.relative_to(ROOT).as_posix() in paths
    assert "dataagent/bootstrap/templates/config.yaml" in paths


def test_installed_wheel_renders_host_prompts(installed_package):
    run, _, _, python, _ = installed_package
    run(
        str(python),
        "-I",
        "-c",
        """
from pathlib import Path

from dataagent.assembly.prompts import (
    render_agent_prompt,
    render_filesystem_prompt,
    render_subagent_prompt,
)

outputs = Path.cwd() / "session outputs"
filesystem = render_filesystem_prompt(outputs=outputs, workspaces=())
assert str(outputs) in filesystem
root = render_agent_prompt(
    filesystem_instructions=filesystem, extension_instructions="root instruction",
)
child = render_subagent_prompt(
    filesystem_instructions=filesystem, subagent_instructions="child instruction",
)
assert filesystem in root and "root instruction" in root
assert filesystem in child and "child instruction" in child
""",
    )


def test_installed_sdk_a2a_boundary_and_server(installed_package):
    run, _, _, python, _ = installed_package
    run(
        str(python),
        "-I",
        "-c",
        """
import sys
from pathlib import Path
import dataagent
import dataagent.host
import dataagent.lowlevel
import dataagent.core
from dataagent.extensions.a2a import A2ACaller
assert not any(name.startswith('dataagent.server') for name in sys.modules)
assert 'fastapi' not in sys.modules
assert Path(dataagent.__file__).is_relative_to(Path(sys.prefix))
for name in dataagent.__all__:
    getattr(dataagent, name)
from importlib.util import find_spec
assert find_spec('serve') is None
assert find_spec('a2a_server') is None
from dataagent.bootstrap.paths import builtin_plugins_path
assert builtin_plugins_path() == Path(dataagent.__file__).parent / 'plugins'
from dataagent.server.app import create_app
from fastapi.testclient import TestClient
with TestClient(create_app(None)) as client:
    assert client.get('/healthz').status_code == 200
    assert client.get('/ready').status_code == 503
""",
    )


def test_installed_command_and_module_entrypoints(installed_package):
    run, _, _, python, bin_dir = installed_package
    cli = bin_dir / ("dataagent.exe" if os.name == "nt" else "dataagent")
    assert (
        run(str(cli), "--help").stdout == run(str(python), "-I", "-m", "dataagent", "--help").stdout
    )
    assert "--workspace" in run(str(cli), "serve", "--help").stdout
