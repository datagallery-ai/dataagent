# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Normalized DataAgent configuration for OpenJiuWen DeepAgent builders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from dataagent.utils.constants import (
    DEFAULT_BUILTIN_SKILL_NAMES,
    DEFAULT_COMPRESS_MESSAGE_CNT,
    DEFAULT_COMPRESS_TOKEN_LIMIT,
)
from dataagent.utils.runtime_paths import dataagent_package_path, resolve_user_root


@dataclass(frozen=True)
class LocalToolSpec:
    """Normalized ``TOOLS.local_functions`` entry."""

    path: str
    module: str
    function: str
    name: str
    description: str | None = None
    category: str = "general"


@dataclass(frozen=True)
class McpServerSpec:
    """Normalized ``TOOLS.mcp_servers`` entry."""

    path: str
    server_id: str
    server_name: str
    client_type: str
    server_path: str
    params: dict[str, Any] = field(default_factory=dict)
    auth_headers: dict[str, Any] = field(default_factory=dict)
    auth_query_params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class A2AAgentSpec:
    """Normalized ``TOOLS.A2A`` / ``TOOLS.a2a`` entry."""

    path: str
    agent_id: str | None
    name: str | None
    url: str
    description: str | None = None
    auth_token: str | None = None
    discovery_timeout: float = 10.0


@dataclass(frozen=True)
class SkillSpec:
    """Normalized ``TOOLS.skills`` configuration."""

    builtin_root: Path
    builtin_allowlist: frozenset[str]
    custom_dirs: tuple[Path, ...]
    user_root: Path

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.builtin_root, *self.custom_dirs, self.user_root)


@dataclass(frozen=True)
class ContextCompressionSpec:
    """Legacy DataAgent thresholds mapped to Jiuwen preset compression."""

    compress_token_limit: int = DEFAULT_COMPRESS_TOKEN_LIMIT
    compress_message_cnt: int = DEFAULT_COMPRESS_MESSAGE_CNT


@dataclass(frozen=True)
class DeepAgentBuildSpec:
    """DataAgent-owned build specification independent of OpenJiuWen objects."""

    enable_human_feedback: bool = False
    context_compression: ContextCompressionSpec = field(
        default_factory=ContextCompressionSpec
    )
    bash_allowlist: tuple[str, ...] | None = None
    local_tools: tuple[LocalToolSpec, ...] = ()
    mcp_servers: tuple[McpServerSpec, ...] = ()
    a2a_agents: tuple[A2AAgentSpec, ...] = ()
    skills: SkillSpec | None = None
    diagnostics: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(cls, config: Any) -> DeepAgentBuildSpec:
        tools_section = _get_config_section(config, "TOOLS")
        enable_human_feedback = _normalize_human_feedback(config)
        context_compression = _normalize_context_compression(config)
        bash_allowlist = _normalize_bash_allowlist(config)
        local_tools, local_diagnostics = _normalize_local_tools(tools_section.get("local_functions"))
        mcp_servers, mcp_diagnostics = _normalize_mcp_servers(tools_section.get("mcp_servers"))
        a2a_agents, a2a_diagnostics = _normalize_a2a_agents(tools_section)
        skills, skill_diagnostics = _normalize_skills(config, tools_section.get("skills"))
        return cls(
            enable_human_feedback=enable_human_feedback,
            context_compression=context_compression,
            bash_allowlist=bash_allowlist,
            local_tools=local_tools,
            mcp_servers=mcp_servers,
            a2a_agents=a2a_agents,
            skills=skills,
            diagnostics=local_diagnostics + mcp_diagnostics + a2a_diagnostics + skill_diagnostics,
        )


def _normalize_context_compression(config: Any) -> ContextCompressionSpec:
    context = _get_config_section(config, "CONTEXT")
    return ContextCompressionSpec(
        compress_token_limit=_positive_int(
            context.get("compress_token_limit"),
            path="CONTEXT.compress_token_limit",
            default=DEFAULT_COMPRESS_TOKEN_LIMIT,
        ),
        compress_message_cnt=_positive_int(
            context.get("compress_message_cnt"),
            path="CONTEXT.compress_message_cnt",
            default=DEFAULT_COMPRESS_MESSAGE_CNT,
        ),
    )


def _positive_int(raw: Any, *, path: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"{path} must be a positive integer")
    return raw


def _normalize_human_feedback(config: Any) -> bool:
    agent_config = _get_config_section(config, "AGENT_CONFIG")
    nested = agent_config.get("enable_human_feedback")
    direct = config.get("enable_human_feedback") if hasattr(config, "get") else None
    for path, raw in (
        ("enable_human_feedback", direct),
        ("AGENT_CONFIG.enable_human_feedback", nested),
    ):
        if raw is not None and not isinstance(raw, bool):
            raise ValueError(f"{path} must be a boolean")
    return direct is True or nested is True


def _normalize_bash_allowlist(config: Any) -> tuple[str, ...] | None:
    raw = config.get("BASH_TOOL_WHITELIST") if hasattr(config, "get") else None
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("BASH_TOOL_WHITELIST must be a list of command names or null")

    commands: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise ValueError(f"BASH_TOOL_WHITELIST[{index}] must be a command name string")
        command = item.strip()
        if not command:
            raise ValueError(f"BASH_TOOL_WHITELIST[{index}] must not be empty")
        if command not in seen:
            commands.append(command)
            seen.add(command)
    return tuple(commands)


def _get_config_section(config: Any, key: str) -> Mapping[str, Any]:
    if isinstance(config, Mapping) or hasattr(config, "get"):
        raw = config.get(key, {})
    else:
        raise TypeError(f"config must be a mapping or provide get(), got {type(config).__name__}")

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{key} must be a mapping, got {type(raw).__name__}")
    return raw


def _normalize_local_tools(raw: Any) -> tuple[tuple[LocalToolSpec, ...], tuple[str, ...]]:
    if raw is None:
        return (), ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("TOOLS.local_functions must be a list of mappings")

    specs: list[LocalToolSpec] = []
    diagnostics: list[str] = []
    names: dict[str, str] = {}

    for index, entry in enumerate(raw):
        path = f"TOOLS.local_functions[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{path} must be a mapping")

        module = _required_string(entry, "module", path)
        function = _required_string(entry, "function", path)
        name = _optional_string(entry, "name", path) or function

        if function == "sub_agent_tool":
            diagnostics.append(
                f"{path} ({module}.{function}) is handled by the OpenJiuWen Subagent adapter and was not "
                "registered as a local Python tool."
            )
            continue

        previous_path = names.get(name)
        if previous_path is not None:
            raise ValueError(f"{path}.name duplicates local tool {name!r} already declared at {previous_path}")
        names[name] = path

        description = _optional_string(entry, "description", path)
        category = _optional_string(entry, "category", path) or "general"

        ignored_fields = [field_name for field_name in ("config", "hooks") if entry.get(field_name)]
        if ignored_fields:
            diagnostics.append(
                f"{path} fields {', '.join(ignored_fields)} are not consumed by the local tool builder yet."
            )

        specs.append(
            LocalToolSpec(
                path=path,
                module=module,
                function=function,
                name=name,
                description=description,
                category=category,
            )
        )

    return tuple(specs), tuple(diagnostics)


def _normalize_mcp_servers(raw: Any) -> tuple[tuple[McpServerSpec, ...], tuple[str, ...]]:
    if raw is None:
        return (), ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError("TOOLS.mcp_servers must be a list of mappings")

    specs: list[McpServerSpec] = []
    diagnostics: list[str] = []
    server_ids: dict[str, str] = {}

    for index, raw_entry in enumerate(raw):
        path = f"TOOLS.mcp_servers[{index}]"
        entry, mapped_name = _normalize_named_mcp_entry(raw_entry, path)
        config = _optional_mapping(entry, "config", path)

        server_name = (
            _optional_string(entry, "server_name", path)
            or _optional_string(entry, "name", path)
            or mapped_name
            or _optional_string(entry, "server_id", path)
        )
        if server_name is None:
            raise ValueError(f"{path} requires server_id, server_name, name, or a named mapping key")
        server_id = _optional_string(entry, "server_id", path) or server_name

        previous_path = server_ids.get(server_id)
        if previous_path is not None:
            raise ValueError(f"{path}.server_id duplicates MCP server {server_id!r} declared at {previous_path}")
        server_ids[server_id] = path

        raw_transport = (
            _optional_string(entry, "transport_type", path)
            or _optional_string(entry, "client_type", path)
            or _optional_string(entry, "transport", path)
        )
        if raw_transport is None:
            raw_transport = "sse" if _first_string(entry, config, ("url", "base_url", "server_path"), path) else "stdio"
        client_type = _normalize_mcp_client_type(raw_transport, path)

        auth_headers = _first_mapping(
            entry,
            config,
            ("auth_headers", "headers"),
            path,
        )
        auth_query_params_raw = _first_mapping(
            entry,
            config,
            ("auth_query_params", "query_params"),
            path,
        )
        auth_query_params = {str(key): str(value) for key, value in auth_query_params_raw.items()}

        if client_type == "stdio":
            command = _first_string(entry, config, ("command",), path)
            if command is None:
                raise ValueError(f"{path}.config.command is required for stdio MCP servers")
            params = _build_stdio_params(entry, config, path, command)
            server_path = _first_string(entry, config, ("server_path",), path) or command
        else:
            server_path = _first_string(entry, config, ("server_path", "url", "base_url"), path)
            if server_path is None:
                raise ValueError(f"{path} requires url or server_path for {client_type} MCP servers")
            params = {
                key: value
                for key, value in config.items()
                if key
                not in {
                    "server_path",
                    "url",
                    "base_url",
                    "auth_headers",
                    "headers",
                    "auth_query_params",
                    "query_params",
                }
            }

        ignored_fields = [field_name for field_name in ("category", "description", "hooks") if entry.get(field_name)]
        if ignored_fields:
            diagnostics.append(
                f"{path} fields {', '.join(ignored_fields)} are not consumed by the MCP adapter yet."
            )

        specs.append(
            McpServerSpec(
                path=path,
                server_id=server_id,
                server_name=server_name,
                client_type=client_type,
                server_path=server_path,
                params=params,
                auth_headers=auth_headers,
                auth_query_params=auth_query_params,
            )
        )

    return tuple(specs), tuple(diagnostics)


def _normalize_a2a_agents(
    tools_section: Mapping[str, Any],
) -> tuple[tuple[A2AAgentSpec, ...], tuple[str, ...]]:
    specs: list[A2AAgentSpec] = []
    diagnostics: list[str] = []
    agent_ids: dict[str, str] = {}
    names: dict[str, str] = {}

    for section_name in ("A2A", "a2a"):
        raw = tools_section.get(section_name)
        if raw is None:
            continue
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"TOOLS.{section_name} must be a list of mappings")

        for index, raw_entry in enumerate(raw):
            path = f"TOOLS.{section_name}[{index}]"
            entry, mapped_name = _normalize_named_a2a_entry(raw_entry, path)

            name = (
                _optional_string(entry, "name", path)
                or mapped_name
                or _optional_string(entry, "agent_id", path)
            )
            agent_id = _optional_string(entry, "agent_id", path) or name
            raw_url = (
                _optional_string(entry, "url", path)
                or _optional_string(entry, "base_url", path)
            )
            if raw_url is None:
                raise ValueError(f"{path} requires url or base_url")
            url = _normalize_a2a_url(raw_url, path)

            if agent_id is not None:
                previous_path = agent_ids.get(agent_id)
                if previous_path is not None:
                    raise ValueError(f"{path}.agent_id duplicates A2A agent {agent_id!r} declared at {previous_path}")
                agent_ids[agent_id] = path

            if name is not None:
                previous_path = names.get(name)
                if previous_path is not None:
                    raise ValueError(f"{path}.name duplicates A2A ability {name!r} declared at {previous_path}")
                names[name] = path

            auth_token = _optional_string(entry, "auth_token", path)
            timeout = _optional_positive_number(entry, "timeout", path) or 10.0
            if auth_token is not None:
                diagnostics.append(
                    f"{path}.auth_token is used for AgentCard discovery only; the current OpenJiuWen "
                    "A2A client does not expose invocation authentication."
                )
            if entry.get("timeout") is not None:
                diagnostics.append(
                    f"{path}.timeout is used for AgentCard discovery only; the current OpenJiuWen "
                    "AbilityManager does not forward it to RemoteAgent.invoke()."
                )
            if entry.get("hooks") is not None:
                diagnostics.append(f"{path}.hooks is deferred to the OpenJiuWen hook adapter.")

            specs.append(
                A2AAgentSpec(
                    path=path,
                    agent_id=agent_id,
                    name=name,
                    url=url,
                    description=_optional_string(entry, "description", path),
                    auth_token=auth_token,
                    discovery_timeout=timeout,
                )
            )

    return tuple(specs), tuple(diagnostics)


def _normalize_skills(config: Any, raw: Any) -> tuple[SkillSpec, tuple[str, ...]]:
    path = "TOOLS.skills"
    if raw is None:
        settings: Mapping[str, Any] = {}
    elif not isinstance(raw, Mapping):
        raise ValueError(f"{path} must be a mapping")
    else:
        settings = raw

    if "user" in settings:
        raise ValueError(
            "TOOLS.skills.user is not supported; user skills are discovered from "
            "~/.dataagent/{user_id}/skills at runtime."
        )

    builtin_raw = settings.get("builtin")
    if builtin_raw is None:
        configured_builtin: set[str] = set()
    else:
        configured_builtin = set(_string_list(builtin_raw, f"{path}.builtin"))
    configured_builtin.update(DEFAULT_BUILTIN_SKILL_NAMES)

    custom_raw = settings.get("custom_dirs")
    builtin_root = dataagent_package_path("actions", "skills")
    custom_dirs: list[Path] = []
    if custom_raw is not None:
        for item in _string_list(custom_raw, f"{path}.custom_dirs"):
            candidate = Path(item).expanduser()
            if not candidate.is_absolute():
                candidate = dataagent_package_path(*candidate.parts)
            resolved = candidate.resolve()
            if resolved != builtin_root and resolved not in custom_dirs:
                custom_dirs.append(resolved)

    config_mapping = config.get_all() if hasattr(config, "get_all") else config
    if not isinstance(config_mapping, Mapping):
        config_mapping = {}

    return (
        SkillSpec(
            builtin_root=builtin_root,
            builtin_allowlist=frozenset(configured_builtin),
            custom_dirs=tuple(custom_dirs),
            user_root=resolve_user_root(config=config_mapping) / "skills",
        ),
        (),
    )


def _string_list(raw: Any, path: str) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise ValueError(f"{path} must be a list of strings")

    values: list[str] = []
    for index, item in enumerate(raw):
        value = str(item).strip()
        if not value:
            raise ValueError(f"{path}[{index}] must not be empty")
        values.append(value)
    return tuple(values)


_MCP_FLAT_FIELDS = {
    "server_id",
    "server_name",
    "name",
    "transport_type",
    "client_type",
    "transport",
    "config",
    "server_path",
    "url",
    "base_url",
    "command",
    "args",
    "env",
    "cwd",
    "encoding_error_handler",
    "auth_headers",
    "headers",
    "auth_query_params",
    "query_params",
    "category",
    "description",
    "hooks",
}


_A2A_FLAT_FIELDS = {
    "agent_id",
    "name",
    "url",
    "base_url",
    "description",
    "auth_token",
    "timeout",
    "hooks",
}


def _normalize_named_mcp_entry(raw_entry: Any, path: str) -> tuple[Mapping[str, Any], str | None]:
    if not isinstance(raw_entry, Mapping):
        raise ValueError(f"{path} must be a mapping")

    if len(raw_entry) == 1:
        mapped_name, mapped_config = next(iter(raw_entry.items()))
        if mapped_name not in _MCP_FLAT_FIELDS and isinstance(mapped_config, Mapping):
            if not isinstance(mapped_name, str) or not mapped_name.strip():
                raise ValueError(f"{path} MCP mapping key must be a non-empty string")
            return mapped_config, mapped_name.strip()

    return raw_entry, None


def _normalize_named_a2a_entry(raw_entry: Any, path: str) -> tuple[Mapping[str, Any], str | None]:
    if not isinstance(raw_entry, Mapping):
        raise ValueError(f"{path} must be a mapping")

    if len(raw_entry) == 1:
        mapped_name, mapped_config = next(iter(raw_entry.items()))
        if mapped_name not in _A2A_FLAT_FIELDS and isinstance(mapped_config, Mapping):
            if not isinstance(mapped_name, str) or not mapped_name.strip():
                raise ValueError(f"{path} A2A mapping key must be a non-empty string")
            return mapped_config, mapped_name.strip()

    return raw_entry, None


def _normalize_a2a_url(raw: str, path: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{path}.url must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{path}.url must not contain query parameters or fragments")

    normalized_path = parsed.path.rstrip("/")
    if normalized_path.endswith("/a2a/jsonrpc"):
        normalized_path = normalized_path[:-12]
    elif normalized_path.endswith("/a2a"):
        normalized_path = normalized_path[:-4]

    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", "")).rstrip("/")


def _optional_positive_number(entry: Mapping[str, Any], key: str, path: str) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{path}.{key} must be a positive number")
    return float(value)


def _normalize_mcp_client_type(raw: str, path: str) -> str:
    normalized = raw.strip().lower().replace("_", "-")
    aliases = {
        "http": "streamable-http",
        "streamablehttp": "streamable-http",
    }
    normalized = aliases.get(normalized, normalized)
    supported = {"stdio", "sse", "streamable-http", "openapi", "playwright"}
    if normalized not in supported:
        raise ValueError(
            f"{path}.transport_type {raw!r} is unsupported; expected one of {sorted(supported)}"
        )
    return normalized


def _build_stdio_params(
    entry: Mapping[str, Any],
    config: Mapping[str, Any],
    path: str,
    command: str,
) -> dict[str, Any]:
    params = dict(config)
    params["command"] = command

    args = _first_value(entry, config, ("args",))
    if args is not None:
        if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
            raise ValueError(f"{path}.config.args must be a list")
        params["args"] = list(args)

    env = _first_value(entry, config, ("env",))
    if env is not None:
        if not isinstance(env, Mapping):
            raise ValueError(f"{path}.config.env must be a mapping")
        params["env"] = dict(env)

    cwd = _first_string(entry, config, ("cwd",), path)
    if cwd is not None:
        params["cwd"] = cwd

    encoding_error_handler = _first_string(entry, config, ("encoding_error_handler",), path)
    if encoding_error_handler is not None:
        params["encoding_error_handler"] = encoding_error_handler

    for key in (
        "server_path",
        "url",
        "base_url",
        "auth_headers",
        "headers",
        "auth_query_params",
        "query_params",
    ):
        params.pop(key, None)
    return params


def _optional_mapping(entry: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any]:
    value = entry.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{key} must be a mapping")
    return value


def _first_value(
    entry: Mapping[str, Any],
    config: Mapping[str, Any],
    keys: tuple[str, ...],
) -> Any:
    for source in (entry, config):
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _first_string(
    entry: Mapping[str, Any],
    config: Mapping[str, Any],
    keys: tuple[str, ...],
    path: str,
) -> str | None:
    value = _first_value(entry, config, keys)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path}.{keys[0]} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{path}.{keys[0]} must not be empty")
    return normalized


def _first_mapping(
    entry: Mapping[str, Any],
    config: Mapping[str, Any],
    keys: tuple[str, ...],
    path: str,
) -> dict[str, Any]:
    value = _first_value(entry, config, keys)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{path}.{keys[0]} must be a mapping")
    return dict(value)


def _required_string(entry: Mapping[str, Any], key: str, path: str) -> str:
    value = _optional_string(entry, key, path)
    if value is None:
        raise ValueError(f"{path}.{key} is required and must be a non-empty string")
    return value


def _optional_string(entry: Mapping[str, Any], key: str, path: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{path}.{key} must not be empty")
    return normalized
