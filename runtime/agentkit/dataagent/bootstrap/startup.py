"""The single startup entry shared by hosts and the SDK: LaunchOptions in, Runtime out.

Order is fixed: Home → (optional) Home initialization → effective environment → configuration →
read-only inputs → extension locations. Nothing here
touches a terminal, HTTP, LangChain or plugin code.
"""

from dataclasses import replace
from pathlib import Path

from dataagent.bootstrap.config_files import ConfigSource, load_layers, merge
from dataagent.bootstrap.discovery import locate_extensions
from dataagent.bootstrap.environment import load_env
from dataagent.bootstrap.mcp import load_mcp_config
from dataagent.bootstrap.options import LaunchOptions
from dataagent.bootstrap.paths import (
    RuntimePaths,
    WorkspaceInput,
    absolute,
    directory,
    home_path,
    initialize_home,
    opaque_id,
    read_session_binding,
    session_paths,
)
from dataagent.bootstrap.runtime import Runtime, StartupReport


def prepare_runtime(options: LaunchOptions | None = None) -> Runtime:
    options = options or LaunchOptions()
    # Step1. Resolve launch inputs; Home initialization is the only optional write here.
    user_id = opaque_id(options.user_id, "user id")
    if options.session_id is not None:
        opaque_id(options.session_id, "session id")
    if options.workspace is not None and options.workspaces:
        raise ValueError("Pass workspace inputs with workspace or workspaces, not both")
    cwd = directory(absolute(options.cwd, Path.cwd()), required=True)
    home = home_path(cwd)
    if options.init_home:
        initialize_home(home)

    cli_config = _explicit_file(options.config, cwd, "Configuration")
    env_file = _explicit_file(options.env_file, cwd, "Environment", must_exist=False)
    env_paths = [home / ".env"]
    if cli_config is not None:
        env_paths.append(cli_config.with_name(".env"))
    environment = load_env(tuple(env_paths), env_file)
    layers = load_layers(home, environment.values, cli_config)
    settings = merge(layers)

    # Step2. Inputs are a list. CLI and DATAAGENT_WORKSPACE override configured defaults.
    # No configured inputs means an empty list; Home already owns the working state.
    # A resumed session keeps the binding recorded when it was created.
    explicit = [options.workspace] if options.workspace is not None else []
    explicit.extend(options.workspaces)
    explicit_inputs = bool(explicit) or bool(environment.values.get("DATAAGENT_WORKSPACE", "").strip())
    if explicit:
        workspaces = tuple(
            WorkspaceInput(f"workspace-{index}", absolute(value, cwd, expand_home=True))
            for index, value in enumerate(explicit)
        )
    elif environment.values.get("DATAAGENT_WORKSPACE", "").strip():
        workspaces = (WorkspaceInput(
            "workspace-0", absolute(environment.values["DATAAGENT_WORKSPACE"], cwd, expand_home=True),
        ),)
    else:
        workspaces = tuple(WorkspaceInput(item.name, item.path) for item in settings.dataagent.workspaces)
    if options.session_id is not None:
        workspaces = _resume_workspaces(
            home, user_id, options.session_id, workspaces, explicit=explicit_inputs,
        )
    paths = RuntimePaths.resolve(home, workspaces, user_id=user_id)

    # Step3. Only Home supplies automatic user extensions; cwd and inputs are data paths.
    for name in ("skills", "hooks", "plugins"):
        directory(home / name)
    extensions, plugin_origins = locate_extensions(paths, layers, settings)
    mcp_roots = tuple(dict.fromkeys(
        root.resolve() for root in (*extensions.plugin_roots, *settings.plugins.paths)
        if root.name in settings.plugins.enabled
    ))
    extensions = replace(extensions, mcp_servers=load_mcp_config(home, mcp_roots, environment.values))

    # Step4. Keep applied configuration sources visible in the returned Runtime.
    applied = {layer.scope for layer in layers}
    configs = [
        ConfigSource("user", home / "config.json", "user" in applied),
    ]
    if cli_config is not None:
        configs.append(ConfigSource("cli", cli_config, True))
    report = StartupReport(
        configs=tuple(configs),
        plugin_origins=plugin_origins,
        env_file=env_file,
    )
    return Runtime(settings=settings, paths=paths, report=report, extensions=extensions)


def refresh_extensions(runtime: Runtime) -> Runtime:
    """Reread extension declarations; retain the host's model, paths and launch policy.

    Pure preparation only. Hosts validate/compile the candidate before publishing it.
    The original Home and explicit configuration/env-file paths remain authoritative.
    """
    home = runtime.paths.home
    cli_config = next((item.path for item in runtime.report.configs if item.scope == "cli"), None)
    env_paths = (home / ".env", *([cli_config.with_name(".env")] if cli_config else []))
    environment = load_env(env_paths, runtime.report.env_file)
    layers = load_layers(home, environment.values, cli_config)
    loaded = merge(layers)
    settings = runtime.settings.model_copy(update={
        "plugins": loaded.plugins,
        "dataagent": runtime.settings.dataagent.model_copy(update={"hooks": loaded.dataagent.hooks}),
    })
    extensions, origins = locate_extensions(runtime.paths, layers, settings)
    roots = tuple(dict.fromkeys(
        root.resolve() for root in (*extensions.plugin_roots, *settings.plugins.paths)
        if root.name in settings.plugins.enabled
    ))
    extensions = replace(extensions, mcp_servers=load_mcp_config(home, roots, environment.values))
    applied = {layer.scope for layer in layers}
    report = replace(runtime.report, plugin_origins=origins, configs=tuple(
        replace(item, applied=item.scope in applied) for item in runtime.report.configs
    ))
    return replace(runtime, settings=settings, extensions=extensions, report=report)


def _resume_workspaces(
    home: Path, user_id: str, thread_id: str, launched: tuple[WorkspaceInput, ...], *, explicit: bool,
) -> tuple[WorkspaceInput, ...]:
    """Use the saved input list. An explicit different list is a startup error, not a rebind."""
    session = session_paths(home, user_id, thread_id)
    if not session.session_file.is_file():
        raise ValueError(
            f"Session {thread_id} is not in profile {user_id}. "
            "Choose an existing session for this profile or start a new one."
        )
    binding = read_session_binding(session)
    if binding.user_id != user_id or binding.thread_id != thread_id:
        raise ValueError(
            f"Session {thread_id} is not in profile {user_id}. "
            "Choose an existing session for this profile or start a new one."
        )
    if explicit:
        launched_paths = tuple(directory(item.path, required=True) for item in launched)
        if launched_paths != tuple(item.path for item in binding.workspaces):
            raise ValueError(
                f"Session {thread_id} is bound to a different workspace list. "
                "Start a new session to use different inputs."
            )
    return binding.workspaces


def _explicit_file(value, cwd: Path, kind: str, *, must_exist: bool = True) -> Path | None:
    if value is None:
        return None
    path = absolute(value, cwd)
    if must_exist and not path.is_file():
        raise ValueError(f"{kind} file does not exist or is not a file: {path}")
    return path
