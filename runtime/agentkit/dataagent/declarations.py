"""External extension declarations shared by product settings and the compiler.

Pure Pydantic: no file IO, environment access or model tooling. ToolSpec references a
Python object; the native tool or function still defines its argument schema.
"""

from collections.abc import Mapping
from typing import Annotated, Literal, get_args

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


# --- Shared validation and extension declarations -------------------------------------------

LOCAL_ID_PATTERN = r"[a-z][a-z0-9_]*"
"""Plugin-local tool IDs."""

RESOURCE_ID_PATTERN = r"[a-z][a-z0-9_-]*"
"""Plugin IDs and SubAgent names."""


class StdioMCPServer(StrictModel):
    type: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, repr=False)


class RemoteMCPServer(StrictModel):
    type: Literal["http", "sse"]
    url: AnyHttpUrl
    headers: dict[str, str] = Field(default_factory=dict, repr=False)


class MCPConfig(StrictModel):
    mcpServers: dict[
        Annotated[str, Field(pattern=f"^{RESOURCE_ID_PATTERN}$")],
        Annotated[StdioMCPServer | RemoteMCPServer, Field(discriminator="type")],
    ]

    @model_validator(mode="before")
    @classmethod
    def default_stdio(cls, value):
        if isinstance(value, Mapping) and isinstance(value.get("mcpServers"), Mapping):
            return {**value, "mcpServers": {
                name: {"type": "stdio", **spec} if isinstance(spec, Mapping) else spec
                for name, spec in value["mcpServers"].items()
            }}
        return value


class ToolSpec(StrictModel):
    """A Python function or native tool reference, not a second tool argument schema."""

    entrypoint: str = Field(min_length=1)


def _json_containers(value):
    """Convert frozen configuration-layer containers, without accepting arbitrary objects."""
    if isinstance(value, Mapping):
        return {key: _json_containers(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_containers(item) for item in value]
    return value


class ExtensionConfig(StrictModel):
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def plain_params(cls, value):
        return _json_containers(value)


HookEvent = Literal[
    "before_agent", "after_agent", "before_model", "after_model",
    "before_tool", "after_tool",
]

HOOK_EVENTS = get_args(HookEvent)


def normalize_hooks(value):
    """Normalize event-grouped Hooks to the compiler's flat declaration shape."""
    if isinstance(value, Mapping):
        entries = []
        for event, declarations in value.items():
            if event not in HOOK_EVENTS:
                raise ValueError(f"Unknown Hook event: {event}")
            if not isinstance(declarations, (list, tuple)):
                raise ValueError(f"Hook event {event} must contain a list")
            for declaration in declarations:
                if not isinstance(declaration, Mapping):
                    raise ValueError(f"Hook declaration for {event} must be a mapping")
                if "event" in declaration:
                    raise ValueError(f"Hook group {event} already declares the event; remove inner event")
                entries.append({**declaration, "event": event})
        return entries
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


class HookSpec(ExtensionConfig):
    """One ordinary Python function attached to a native middleware lifecycle point."""

    event: HookEvent
    entrypoint: str = Field(min_length=1)
    matcher: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_protocol(cls, value):
        if isinstance(value, Mapping) and "config" in value:
            raise ValueError("Hook params field is now `params`; rename `config` to `params`")
        if isinstance(value, Mapping) and set(value) & {
            "command", "timeout_seconds", "handler", "events", "match",
        }:
            raise ValueError(
                "Hooks support Python functions only: use event + entrypoint + params; "
                "command, per-hook timeout_seconds and handler/events/match are unsupported"
            )
        return value

    @model_validator(mode="after")
    def validate_matcher(self):
        if self.matcher is not None and self.event not in {"before_tool", "after_tool"}:
            raise ValueError("Hook matcher is only supported for before_tool and after_tool")
        if self.matcher is not None and not self.matcher.strip():
            raise ValueError("Hook matcher must not be blank")
        return self


RESERVED_PLUGIN_IDS = frozenset({"user", "cli"})
"""Product configuration scopes cannot also name plugins."""


class SubAgentSpec(StrictModel):
    name: str = Field(pattern=f"^{RESOURCE_ID_PATTERN}$")
    description: str = Field(min_length=1)
    system_prompt: str
    tools: list[str] | None = None
    # Omitted sources inherit the root's compiled Skills; [] explicitly opts out.
    skills: list[str] | None = None
    hooks: list[HookSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_hook_groups(cls, value):
        if isinstance(value, Mapping) and "hooks" in value:
            return {**value, "hooks": normalize_hooks(value["hooks"])}
        return value


class PluginSpec(StrictModel):
    schema_version: Literal[2] = 2
    id: str = Field(pattern=f"^{RESOURCE_ID_PATTERN}$")
    system_prompt: str | None = None
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    subagents: list[str] = Field(default_factory=list)
    hooks: list[HookSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_hooks(cls, value):
        if isinstance(value, dict) and "hooks" in value:
            value = {**value, "hooks": normalize_hooks(value["hooks"])}
        hooks = value.get("hooks", []) if isinstance(value, dict) else []
        hooks = hooks if isinstance(hooks, (list, tuple)) else []
        if isinstance(value, dict) and (
            value.get("schema_version", 2) != 2 or "hook_handlers" in value
            or any(isinstance(item, dict) and ("handler" in item or "events" in item)
                   for item in hooks)
        ):
            raise ValueError(
                "Migrate plugin to schema_version: 2 and hooks with event plus "
                "Python entrypoint; hook_handlers and handler/events are no longer supported"
            )
        return value

    @model_validator(mode="after")
    def reserved_id(self):
        if self.id in RESERVED_PLUGIN_IDS:
            raise ValueError(f"Reserved plugin ID: {self.id}")
        return self
