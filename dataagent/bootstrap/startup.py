"""The single startup entry shared by hosts and the SDK: LaunchOptions in, PreparedRuntime out.

Order is fixed: Home → (optional) Home initialization → effective environment → configuration →
read-only inputs → extension locations. Nothing here
touches a terminal, HTTP, LangChain or plugin code.
"""

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from dataagent.bootstrap.config_files import (
    ENV_REFERENCE,
    ConfigSource,
    _load_layer,
    expand_env,
    load_layers,
    merge,
)
from dataagent.bootstrap.discovery import locate_extensions
from dataagent.bootstrap.environment import load_env, selected_env_path
from dataagent.bootstrap.mcp import load_mcp_config
from dataagent.bootstrap.options import LaunchOptions
from dataagent.bootstrap.paths import (
    RuntimePaths,
    WorkspaceInput,
    absolute,
    directory,
    find_session_binding,
    home_path,
    initialize_home,
    opaque_id,
    session_root,
    validate_workspace_binding,
)
from dataagent.bootstrap.runtime import PreparedRuntime, StartupReport
from dataagent.config.plugins import contained_path, resolve_plugins
from dataagent.config.settings import ServerSettings
from dataagent.config.strict_yaml import check_yaml_path, read_yaml


def prepare_runtime(
    options: LaunchOptions | None = None, *, preview_home: bool = False
) -> PreparedRuntime:
    """Prepare effective configuration and declarations without executing plugin Python code."""
    options = options or LaunchOptions()
    # Step1. Resolve launch inputs; Home initialization is the only optional write here.
    user_id = opaque_id(options.user_id, "user id")
    if options.session_id is not None:
        opaque_id(options.session_id, "session id")
    cwd = directory(absolute(options.cwd, Path.cwd()), required=True)
    home = home_path(cwd)
    if options.init_home:
        initialize_home(home, initialize_config=options.config is None)

    cli_config = _explicit_file(options.config, cwd)
    env_file = _explicit_file(options.env_file, cwd)
    dotenv = selected_env_path(home, env_file)
    environment = load_env(() if env_file is not None else (dotenv,), env_file)
    layers = load_layers(
        home, environment.values, cli_config, dotenv=dotenv, preview_home=preview_home
    )
    settings = merge(layers)

    # Step2. Inputs are a list. CLI and DATAAGENT_WORKSPACE override configured defaults.
    # No configured inputs means an empty list; Home already owns the working state.
    # A resumed session keeps the binding recorded when it was created.
    explicit = list(options.workspaces)
    explicit_inputs = bool(explicit) or bool(
        environment.values.get("DATAAGENT_WORKSPACE", "").strip()
    )
    if explicit:
        workspaces = tuple(
            WorkspaceInput(f"workspace-{index}", absolute(value, cwd, expand_home=True))
            for index, value in enumerate(explicit)
        )
    elif environment.values.get("DATAAGENT_WORKSPACE", "").strip():
        workspaces = (
            WorkspaceInput(
                "workspace-0",
                absolute(environment.values["DATAAGENT_WORKSPACE"], cwd, expand_home=True),
            ),
        )
    else:
        workspaces = tuple(
            WorkspaceInput(item.name, item.path) for item in settings.dataagent.workspaces
        )
    if options.session_id is not None:
        workspaces = _resume_workspaces(
            home,
            user_id,
            options.session_id,
            workspaces,
            explicit=explicit_inputs,
        )
    paths = RuntimePaths.resolve(
        home,
        workspaces,
        user_id=user_id,
        output_root=settings.dataagent.output_root,
    )

    # Step3. Only Home supplies automatic user extensions; cwd and inputs are data paths.
    for name in ("skills", "hooks", "plugins"):
        directory(home / name)
    settings, extensions, plugin_origins, plugin_sources = _configure_plugins(
        paths, layers, settings, environment.values, dotenv
    )
    mcp_roots = tuple(
        dict.fromkeys(
            root.resolve()
            for root in (*extensions.plugin_roots, *settings.plugins.paths)
            if root.name in settings.plugins.enabled
        )
    )
    extensions = replace(
        extensions, mcp_servers=load_mcp_config(home, mcp_roots, environment.values, dotenv=dotenv)
    )

    # Step4. Keep applied configuration sources visible in the returned PreparedRuntime.
    applied = {layer.scope for layer in layers}
    configs = [
        ConfigSource("user", home / "config.yaml", "user" in applied),
    ]
    if cli_config is not None:
        configs.append(ConfigSource("cli", cli_config, True))
    report = StartupReport(
        configs=(*plugin_sources, *configs),
        plugin_origins=plugin_origins,
        env_file=env_file,
    )
    # Retain only model-referenced values, not the entire process environment. Resolve
    # secondary models on first use without rereading dotenv or changing os.environ.
    model_environment = MappingProxyType(
        {
            name: environment.values[name]
            for model in settings.models.values()
            for value in (model.name, model.base_url, model.api_key.get_secret_value())
            for name in ENV_REFERENCE.findall(value)
            if name in environment.values
        }
    )
    return PreparedRuntime(
        settings=settings,
        paths=paths,
        report=report,
        extensions=extensions,
        model_environment=model_environment,
        a2a_auth_token=environment.values.get("DATAAGENT_A2A_AUTH_TOKEN", "").strip() or None,
    )


def refresh_extensions(runtime: PreparedRuntime) -> PreparedRuntime:
    """Reread extension declarations; retain the host's model, paths and launch policy.

    Pure preparation only. Hosts validate/compile the candidate before publishing it.
    The original Home and explicit configuration/env-file paths remain authoritative.
    """
    home = runtime.paths.home
    cli_config = next((item.path for item in runtime.report.configs if item.scope == "cli"), None)
    env_file = runtime.report.env_file
    dotenv = selected_env_path(home, env_file)
    environment = load_env(() if env_file is not None else (dotenv,), env_file)
    layers = load_layers(home, environment.values, cli_config, dotenv=dotenv)
    loaded = merge(layers)
    settings = runtime.settings.model_copy(
        update={
            "plugins": loaded.plugins,
            "a2a": loaded.a2a,
            "dataagent": runtime.settings.dataagent.model_copy(
                update={"hooks": loaded.dataagent.hooks}
            ),
        }
    )
    settings, extensions, origins, plugin_sources = _configure_plugins(
        runtime.paths, layers, settings, environment.values, dotenv
    )
    roots = tuple(
        dict.fromkeys(
            root.resolve()
            for root in (*extensions.plugin_roots, *settings.plugins.paths)
            if root.name in settings.plugins.enabled
        )
    )
    extensions = replace(
        extensions, mcp_servers=load_mcp_config(home, roots, environment.values, dotenv=dotenv)
    )
    applied = {layer.scope for layer in layers}
    report = replace(
        runtime.report,
        plugin_origins=origins,
        configs=(
            *plugin_sources,
            *(
                replace(item, applied=item.scope in applied)
                for item in runtime.report.configs
                if item.scope != "plugin"
            ),
        ),
    )
    return replace(runtime, settings=settings, extensions=extensions, report=report)


def _resume_workspaces(
    home: Path,
    user_id: str,
    thread_id: str,
    launched: tuple[WorkspaceInput, ...],
    *,
    explicit: bool,
) -> tuple[WorkspaceInput, ...]:
    """Use the saved input list. An explicit different list is a startup error, not a rebind."""
    binding = find_session_binding(session_root(home, user_id, thread_id))
    if binding is None:
        raise ValueError(
            f"Session {thread_id} does not belong to user {user_id}. "
            "Choose one of this user's sessions or start a new one."
        )
    if binding.user_id != user_id or binding.thread_id != thread_id:
        raise ValueError(
            f"Session {thread_id} does not belong to user {user_id}. "
            "Choose one of this user's sessions or start a new one."
        )
    validate_workspace_binding(binding, launched if explicit else None)
    return binding.workspaces


def server_listen(options: LaunchOptions | None = None) -> tuple[str, int]:
    """Listen address of the selected config file, usable when prepare_runtime fails.

    Reads only the `server` section of `options.config` or Home `config.yaml` and expands
    `$env{NAME}` with the same environment as prepare_runtime. Invalid configuration is never replaced with defaults.
    """
    options = options or LaunchOptions()
    cwd = absolute(options.cwd, Path.cwd())
    home = home_path(cwd)
    path = _explicit_file(options.config, cwd) or home / "config.yaml"
    if options.config is None and not path.exists():
        return ServerSettings().host, ServerSettings().port
    raw = read_yaml(path)
    env_file = _explicit_file(options.env_file, cwd)
    dotenv = selected_env_path(home, env_file)
    environment = load_env(() if env_file is not None else (dotenv,), env_file)
    section = expand_env(
        raw.get("server", {}),
        environment.values,
        source=path,
        home=home,
        location=("server",),
        dotenv=dotenv,
    )
    server = ServerSettings.model_validate(section)
    return server.host, server.port


def _explicit_file(value, cwd: Path) -> Path | None:
    if value is None:
        return None
    return absolute(value, cwd)


def _configure_plugins(paths, layers, settings, environment, dotenv):
    candidates, _ = locate_extensions(paths, layers, settings)
    selected = resolve_plugins(settings.plugins, candidates.plugin_roots)
    defaults = []
    for root, spec in selected:
        if spec.defaults_file is not None:
            check_yaml_path(Path(spec.defaults_file))
            layer = _load_layer(
                "plugin",
                contained_path(root, spec.defaults_file),
                env=environment,
                home=paths.home,
                dotenv=dotenv,
            )
            defaults.append(replace(layer, base_dir=root))
    combined = (*defaults, *layers)
    effective = merge(combined)
    # Refresh only replaces plugin settings; host models, input paths and policy stay fixed.
    plugins = effective.plugins.model_copy(
        update={"enabled": tuple(spec.id for _, spec in selected)}
    )
    settings = settings.model_copy(update={"plugins": plugins})
    locations, origins = locate_extensions(paths, combined, settings)
    sources = tuple(ConfigSource("plugin", layer.path, True) for layer in defaults)
    return settings, locations, origins, sources
