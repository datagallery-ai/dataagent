"""Check dependency direction and public imports without loading product modules."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "dataagent"
LEAVES = {
    "dataagent.config.declarations",
    "dataagent.config.strict_yaml",
    "dataagent.diagnostics.errors",
    "dataagent.diagnostics.render",
}
FOUNDATION = LEAVES | {
    "dataagent.config.settings",
    "dataagent.config.plugins",
    "dataagent.diagnostics.logging",
}
MODEL_TOOLING = ("langchain", "langgraph", "deepagents", "ag_ui", "mcp")
EXTENSION_DEPENDENCIES = {
    "a2a": set(),
    "loop_detection": set(),
    "tracing": {"trace_exporter"},
    "trace_exporter": set(),
    "mcp": {"loading"},
    "filesystem_backend": set(),
    "loading": set(),
    "skills": {"loading"},
    "tools": {"loading"},
    "hooks": {"loading"},
    "hitl": {"loading"},
    "run_hooks": {"hooks"},
    "subagents": {"loading", "skills", "tools", "hooks", "run_hooks"},
    "compiler": {"loading", "skills", "tools", "subagents", "hooks", "hitl"},
    "__init__": {"compiler", "loading"},
}
EXTENSION_CORE_DEPENDENCIES = {
    "a2a": {"config.declarations", "diagnostics.errors"},
    "loop_detection": {"diagnostics.errors"},
    "tracing": {"diagnostics.render", "diagnostics.logging"},
    "trace_exporter": {"diagnostics.render"},
    "mcp": {"bootstrap", "diagnostics.render"},
    "filesystem_backend": {"bootstrap", "bootstrap.paths", "core.backends"},
    "loading": {"config.declarations", "config.settings", "config.plugins"},
    "skills": {"core.middleware.skills"},
    "tools": {"config.declarations"},
    "hooks": {"config.declarations"},
    "hitl": {"config.declarations"},
    "run_hooks": set(),
    "subagents": {"config.declarations", "config.strict_yaml", "semantic_client"},
    "compiler": {"config.declarations", "semantic_client"},
    "__init__": set(),
}


def assert_extension_dependencies(path: Path, *, root: Path = ROOT):
    allowed = EXTENSION_DEPENDENCIES[path.stem]
    for module in imports(path, root=root):
        if module.startswith("dataagent.extensions"):
            target = module.removeprefix("dataagent.extensions.")
            assert target in allowed, (path, target)
        elif module == "dataagent" or module.startswith("dataagent."):
            target = module.removeprefix("dataagent.")
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
                module = ".".join(
                    (*parents[: len(parents) - node.level + 1], *filter(None, module.split(".")))
                )
            for alias in node.names:
                candidate = f"{module}.{alias.name}"
                location = root.joinpath(*candidate.split("."))
                # On case-insensitive filesystems PreparedRuntime must not match runtime.py.
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
        path = ROOT.joinpath(*name.split(".")).with_suffix(".py")
        assert core_imports(path) == set(), path


def test_settings_depends_only_on_declarations():
    assert core_imports(CORE / "config/settings.py") == {"dataagent.config.declarations"}


@pytest.mark.parametrize("package", ["config", "diagnostics"])
def test_foundation_packages_only_import_their_own_modules(package):
    for path in (CORE / package).glob("*.py"):
        for module in imports(path):
            assert not module.startswith(MODEL_TOOLING), (path, module)
            if module == "dataagent" or module.startswith("dataagent."):
                assert module.startswith(f"dataagent.{package}."), (path, module)


def test_bootstrap_depends_only_on_foundation_and_never_on_model_tooling():
    for path in (CORE / "bootstrap").glob("*.py"):
        for module in imports(path):
            assert not module.startswith(MODEL_TOOLING), (path, module)
            if module.startswith("dataagent"):
                assert module in FOUNDATION or module.startswith("dataagent.bootstrap"), (
                    path,
                    module,
                )


def test_bootstrap_types_live_with_their_concern_without_reverse_dependencies():
    # paths is the bottom of bootstrap: leaf modules only, never settings or config files.
    assert core_imports(CORE / "bootstrap/paths.py") <= LEAVES
    assert core_imports(CORE / "bootstrap/options.py") == set()
    assert core_imports(CORE / "bootstrap/discovery.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths",
        "dataagent.bootstrap.config_files",
        "dataagent.bootstrap.environment",
    }
    assert core_imports(CORE / "bootstrap/config_files.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths",
    }
    assert core_imports(CORE / "bootstrap/runtime.py") <= FOUNDATION | {
        "dataagent.bootstrap.paths",
        "dataagent.bootstrap.discovery",
        "dataagent.bootstrap.config_files",
    }


def test_bootstrap_class_ownership_is_explicit():
    expected = {
        "options": {"LaunchOptions"},
        "runtime": {"PreparedRuntime", "StartupReport"},
        "discovery": {"ExtensionLocations", "HookBinding", "RunHookBinding"},
        "config_files": {
            "ConfigSource",
            "ConfigurationLoadError",
            "Layer",
            "MissingEnvironmentError",
        },
    }
    for module, names in expected.items():
        tree = ast.parse((CORE / f"bootstrap/{module}.py").read_text())
        assert {node.name for node in tree.body if isinstance(node, ast.ClassDef)} == names


def test_only_host_adapters_consume_runtime_and_bootstrap_paths():
    rewritten_core = {
        "filesystem_backend": "dataagent.core",
        "skills": "dataagent.core",
    }
    for path in (CORE / "extensions").glob("*.py"):
        host_types = {
            "filesystem_backend": {"dataagent.bootstrap", "dataagent.bootstrap.paths"},
            "mcp": {"dataagent.bootstrap"},
            # Host-owned semantic client is threaded through compile → subagents.
            "compiler": {"dataagent.semantic_client"},
            "subagents": {"dataagent.semantic_client"},
        }.get(path.stem, set())
        core_root = rewritten_core.get(path.stem)
        for module in core_imports(path):
            uses_core = core_root is not None and (
                module == core_root or module.startswith(core_root + ".")
            )
            assert (
                module in FOUNDATION | host_types
                or module.startswith("dataagent.extensions.")
                or uses_core
            ), (path, module)


def test_rest_host_uses_only_the_public_and_host_interfaces():
    for path in [*(CORE / "server").rglob("*.py"), CORE / "cli.py"]:
        source = path.read_text()
        for module in imports(path):
            assert not module.startswith(("yaml", "dotenv")), (path, module)
            if module.startswith("dataagent") and not module.startswith("dataagent.server"):
                assert module in {"dataagent", "dataagent.host"}, (path, module)
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
        "import sys, dataagent, dataagent.bootstrap, dataagent.config.declarations\n"
        "assert not home.exists()\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(%r))\n"
        "assert not loaded, loaded\n"
        "import dataagent.host\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(%r))\n"
        "assert not loaded, loaded\n"
        "from dataagent.lowlevel import build_graph\n"
        "assert any(m.startswith('langchain') for m in sys.modules)\n"
    ) % (MODEL_TOOLING, MODEL_TOOLING)
    subprocess.run([sys.executable, "-c", script], check=True, cwd=ROOT)


def test_runtime_preparation_has_one_public_entrypoint():
    import dataagent
    from dataagent import bootstrap
    from dataagent.bootstrap.startup import prepare_runtime

    assert dataagent.prepare_runtime is bootstrap.prepare_runtime is prepare_runtime
    assert "prepare_runtime" in dataagent.__all__
    assert "prepare_runtime" in bootstrap.__all__


@pytest.mark.parametrize(
    "source,target",
    [
        ("import dataagent.extensions.tools as tools", "dataagent.extensions.tools"),
        ("from dataagent.extensions.tools import resolve_tool_refs", "dataagent.extensions.tools"),
        ("from dataagent.extensions import tools", "dataagent.extensions.tools"),
        ("from .tools import resolve_tool_refs", "dataagent.extensions.tools"),
        ("from . import tools", "dataagent.extensions.tools"),
        ("from ..config.settings import Limits", "dataagent.config.settings"),
        ("from dataagent.extensions import compile_extensions", "dataagent.extensions"),
        ("from dataagent.extensions.tools import BaseTool", "dataagent.extensions.tools"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from . import tools",
            "dataagent.extensions.tools",
        ),
    ],
)
def test_import_resolver_handles_modules_without_importing_them(tmp_path, source, target):
    package = tmp_path / "dataagent/extensions"
    package.mkdir(parents=True)
    (package / "tools.py").write_text("raise RuntimeError('must not import')")
    path = package / "subagents.py"
    path.write_text(source)
    assert target in imports(path, root=tmp_path)
    assert all(
        not name.endswith(("compile_extensions", "BaseTool", "Limits"))
        for name in imports(path, root=tmp_path)
    )


def test_cli_help_does_not_load_model_tooling():
    script = (
        "import sys\nfrom dataagent.cli import main\n"
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
    source.write_text("from dataagent.bootstrap import PreparedRuntime")
    assert imports(source, root=tmp_path) == ["dataagent.bootstrap"]


def test_extension_concerns_follow_dependency_table():
    for path in (CORE / "extensions").glob("*.py"):
        assert_extension_dependencies(path)


@pytest.mark.parametrize(
    "source",
    [
        "import dataagent.extensions.subagents",
        "from dataagent.extensions.subagents import materialize_subagents",
        "from dataagent.extensions import subagents",
        "from .subagents import materialize_subagents",
        "from . import subagents",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n from . import subagents",
        "from .compiler import compile_extensions",
    ],
)
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


@pytest.mark.parametrize(
    "source",
    [
        "from dataagent.assembly.graph import build_native_middleware",
        "from ..assembly.graph import build_native_middleware",
        "from dataagent.lowlevel import build_graph",
        "from dataagent.diagnostics.logging import configure_logging",
    ],
)
@pytest.mark.parametrize("module", ["loading", "hooks"])
def test_dependency_guard_rejects_undeclared_core_edges(tmp_path, source, module):
    path = tmp_path / f"dataagent/extensions/{module}.py"
    path.parent.mkdir(parents=True)
    path.write_text(source)
    with pytest.raises(AssertionError):
        assert_extension_dependencies(path, root=tmp_path)


def test_public_sdk_exports_only_the_high_level_path():
    import dataagent
    import dataagent.host
    import dataagent.lowlevel

    assert set(dataagent.__all__) == {
        "Agent",
        "ConfigurationLoadError",
        "DataAgentError",
        "LaunchOptions",
        "LoopDetectedError",
        "AgentContext",
        "HITLContext",
        "HITLHandler",
        "HITLState",
        "InteractionRequest",
        "ResumeDecision",
        "TriggerDecision",
        "PreparedRuntime",
        "RecallError",
        "RecallOptions",
        "SemanticServiceClient",
        "SubagentServices",
        "build_sql_service",
        "collect_model_stream",
        "create_agent",
        "load_mcp_tools",
        "prepare_runtime",
        "recall_database",
        "safe_error",
        "read_yaml",
    }
    assert set(dataagent.host.__all__) == {
        "ERROR_CODES",
        "OPAQUE_ID_PATTERN",
        "error_code",
        "extension_revision",
        "refresh_extensions",
        "ServerSettings",
        "opaque_id",
        "log_event",
        "server_listen",
        "configure_logging",
        "diagnostic_traceback",
        "log_context",
        "register_log_secrets",
        "safe_interrupt_fields",
    }
    assert dataagent.lowlevel.__all__ == ["build_graph"]
    for name in ("build_graph", "load_extensions", "refresh_extensions", "error_code"):
        assert not hasattr(dataagent, name), name


def test_builtin_plugins_and_examples_use_only_public_modules():
    command = CORE / "plugins/de-agent/scripts/history_sql.py"
    for base in (ROOT / "dataagent/plugins", ROOT / "examples"):
        for path in base.rglob("*.py"):
            for module in imports(path, root=path.parent):
                if module == "dataagent" or module.startswith("dataagent."):
                    assert module == "dataagent" or (
                        path == command and module == "dataagent.host"
                    ), (path, module)
