"""Static checks that the package layout keeps its dependency direction.

    declarations / strict_json / diagnostics   leaves, import nothing from dataagent
    settings                                  -> declarations
    bootstrap                                 -> foundation only; never LangChain
    extensions                                -> foundation; filesystem host adapter -> bootstrap paths
    agent                                   -> everything
    restapi                                 -> the `dataagent` package root only
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "dataagent"
LEAVES = {"dataagent.declarations", "dataagent.strict_json", "dataagent.diagnostics"}
FOUNDATION = LEAVES | {"dataagent.settings"}
MODEL_TOOLING = ("langchain", "langgraph", "deepagents", "ag_ui", "mcp")
EXTENSION_DEPENDENCIES = {
    "tracing": {"trace_exporter"},
    "trace_exporter": set(),
    "mcp": {"loading"},
    "filesystem_backend": set(),
    "loading": set(),
    "skills": {"loading"},
    "tools": {"loading"},
    "hooks": {"loading"},
    "subagents": {"loading", "skills", "tools", "hooks"},
    "compiler": {"loading", "skills", "tools", "subagents", "hooks"},
    "__init__": {"compiler", "loading"},
}
EXTENSION_CORE_DEPENDENCIES = {
    "tracing": {"diagnostics"},
    "trace_exporter": set(),
    "mcp": {"bootstrap", "diagnostics"},
    "filesystem_backend": {"bootstrap"},
    "loading": {"declarations", "settings", "strict_json"},
    "skills": set(),
    "tools": {"declarations"},
    "hooks": {"declarations"},
    "subagents": {"declarations", "strict_json"},
    "compiler": {"declarations"},
    "__init__": set(),
}


def assert_extension_dependencies(path: Path, *, root: Path = ROOT):
    allowed = EXTENSION_DEPENDENCIES[path.stem]
    for module in imports(path, root=root):
        if module.startswith("dataagent.extensions"):
            target = module.removeprefix("dataagent.extensions.")
            assert target in allowed, (path, target)
        elif module == "dataagent" or module.startswith("dataagent."):
            target = module.removeprefix("dataagent.").split(".")[0]
            assert target in EXTENSION_CORE_DEPENDENCIES[path.stem], (path, target)


def imports(path: Path, *, root: Path = ROOT) -> list[str]:
    """Resolve module imports statically, including package-member and relative imports."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parents = path.relative_to(root).parts[:-1]
                module = ".".join((*parents[:len(parents) - node.level + 1],
                                   *filter(None, module.split("."))))
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                location = root.joinpath(*candidate.split("."))
                # On case-insensitive filesystems Runtime must not match runtime.py.
                is_module = location.with_suffix(".py").is_file() and location.name + ".py" in {
                    child.name for child in location.parent.iterdir()
                }
                if is_module or (location / "__init__.py").is_file():
                    found.append(candidate)
                else:
                    found.append(module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


def core_imports(path: Path) -> set[str]:
    return {module for module in imports(path) if module.startswith("dataagent")}


def test_leaves_import_nothing_from_the_package():
    for name in LEAVES:
        path = CORE / f"{name.split('.')[-1]}.py"
        assert core_imports(path) == set(), path


def test_settings_depends_only_on_declarations():
    assert core_imports(CORE / "settings.py") == {"dataagent.declarations"}


def test_bootstrap_depends_only_on_foundation_and_never_on_model_tooling():
    for path in (CORE / "bootstrap").glob("*.py"):
        for module in imports(path):
            assert not module.startswith(MODEL_TOOLING), (path, module)
            if module.startswith("dataagent"):
                assert module in FOUNDATION or module.startswith("dataagent.bootstrap"), (path, module)


def test_bootstrap_types_live_with_their_concern_without_reverse_dependencies():
    assert core_imports(CORE / "bootstrap/paths.py") == set()
    assert core_imports(CORE / "bootstrap/options.py") == set()
    assert core_imports(CORE / "bootstrap/discovery.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths", "dataagent.bootstrap.config_files",
    }
    assert core_imports(CORE / "bootstrap/config_files.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths",
    }
    assert core_imports(CORE / "bootstrap/runtime.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths", "dataagent.bootstrap.discovery", "dataagent.bootstrap.config_files",
    }


def test_bootstrap_class_ownership_is_explicit():
    expected = {
        "options": {"LaunchOptions"},
        "runtime": {"Runtime", "StartupReport"},
        "discovery": {"ExtensionLocations", "HookBinding"},
        "config_files": {"ConfigSource", "Layer"},
    }
    for module, names in expected.items():
        tree = ast.parse((CORE / f"bootstrap/{module}.py").read_text())
        assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == names


def test_only_host_adapters_consume_runtime_and_bootstrap_paths():
    for path in (CORE / "extensions").glob("*.py"):
        host_types = {
            "filesystem_backend": {"dataagent.bootstrap", "dataagent.bootstrap.paths"},
            "mcp": {"dataagent.bootstrap"},
        }.get(path.stem, set())
        for module in core_imports(path):
            assert (module in FOUNDATION | host_types or module.startswith("dataagent.extensions.")), (path, module)


def test_rest_host_uses_only_the_package_root():
    for path in (ROOT / "restapi").glob("*.py"):
        source = path.read_text()
        for module in imports(path):
            assert not module.startswith(("yaml", "dotenv")), (path, module)
            if module.startswith("dataagent"):
                assert module == "dataagent", (path, module)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert ".dataagent" not in node.value, path
        assert "settings.models" not in source and "Path.home()" not in source


def test_importing_the_core_and_bootstrap_layer_does_not_load_model_tooling():
    script = (
        "import os\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['DATAAGENT_HOME'])\n"
        "assert not home.exists()\n"
        "import sys, dataagent, dataagent.bootstrap, dataagent.declarations\n"
        "assert not home.exists()\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(%r))\n"
        "assert not loaded, loaded\n"
        "from dataagent import build_agent\n"
        "assert any(m.startswith('langchain') for m in sys.modules)\n"
    ) % (MODEL_TOOLING,)
    subprocess.run([sys.executable, "-c", script], check=True, cwd=ROOT)


def test_runtime_preparation_has_one_public_entrypoint():
    import dataagent
    from dataagent import bootstrap
    from dataagent.bootstrap.startup import prepare_runtime

    assert dataagent.prepare_runtime is bootstrap.prepare_runtime is prepare_runtime
    assert "prepare_runtime" in dataagent.__all__
    assert "prepare_runtime" in bootstrap.__all__


@pytest.mark.parametrize("source,target", [
    ("import dataagent.extensions.tools as tools", "dataagent.extensions.tools"),
    ("from dataagent.extensions.tools import resolve_tool_refs", "dataagent.extensions.tools"),
    ("from dataagent.extensions import tools", "dataagent.extensions.tools"),
    ("from .tools import resolve_tool_refs", "dataagent.extensions.tools"),
    ("from . import tools", "dataagent.extensions.tools"),
    ("from ..settings import Limits", "dataagent.settings"),
    ("from dataagent.extensions import compile_extensions", "dataagent.extensions"),
    ("from dataagent.extensions.tools import BaseTool", "dataagent.extensions.tools"),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from . import tools", "dataagent.extensions.tools"),
])
def test_import_resolver_handles_modules_without_importing_them(tmp_path, source, target):
    package = tmp_path / "dataagent/extensions"
    package.mkdir(parents=True)
    (package / "tools.py").write_text("raise RuntimeError('must not import')")
    path = package / "subagents.py"
    path.write_text(source)
    assert target in imports(path, root=tmp_path)
    assert all(not name.endswith(("compile_extensions", "BaseTool", "Limits"))
               for name in imports(path, root=tmp_path))


def test_cli_help_does_not_load_model_tooling():
    script = (
        "import sys\nfrom restapi.__main__ import main\n"
        "try:\n main(['--help'])\n"
        "except SystemExit as error:\n assert error.code == 0\n"
        "assert not [m for m in sys.modules if m.startswith(%r)]\n"
    ) % (MODEL_TOOLING,)
    subprocess.run([sys.executable, "-c", script], check=True, cwd=ROOT, capture_output=True)


def test_import_resolver_distinguishes_class_from_lowercase_module(tmp_path):
    package = tmp_path / "dataagent/bootstrap"
    package.mkdir(parents=True)
    (package / "runtime.py").touch()
    source = tmp_path / "consumer.py"
    source.write_text("from dataagent.bootstrap import Runtime")
    assert imports(source, root=tmp_path) == ["dataagent.bootstrap"]


def test_extension_concerns_follow_dependency_table():
    for path in (CORE / "extensions").glob("*.py"):
        assert_extension_dependencies(path)


@pytest.mark.parametrize("source", [
    "import dataagent.extensions.subagents",
    "from dataagent.extensions.subagents import materialize_subagents",
    "from dataagent.extensions import subagents",
    "from .subagents import materialize_subagents",
    "from . import subagents",
    "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from . import subagents",
    "from .compiler import compile_extensions",
])
def test_dependency_guard_rejects_reverse_edges(tmp_path, source):
    package = tmp_path / "dataagent/extensions"
    package.mkdir(parents=True)
    for name in ("subagents", "compiler"):
        (package / f"{name}.py").write_text("raise RuntimeError('must not import')")
    path = package / "tools.py"
    path.write_text(source)
    with pytest.raises(AssertionError):
        assert_extension_dependencies(path, root=tmp_path)


def test_dependency_guard_accepts_declared_subagent_edge(tmp_path):
    package = tmp_path / "dataagent/extensions"
    package.mkdir(parents=True)
    (package / "tools.py").touch()
    path = package / "subagents.py"
    path.write_text("from . import tools")
    assert_extension_dependencies(path, root=tmp_path)


@pytest.mark.parametrize("source", [
    "from dataagent.agent import build_native_middleware",
    "from ..agent import build_native_middleware",
    "from dataagent import build_agent",
])
@pytest.mark.parametrize("module", ["loading", "hooks"])
def test_dependency_guard_rejects_undeclared_core_edges(tmp_path, source, module):
    path = tmp_path / f"dataagent/extensions/{module}.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    with pytest.raises(AssertionError):
        assert_extension_dependencies(path, root=tmp_path)
