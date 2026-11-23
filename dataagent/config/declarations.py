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

NAME_PATTERN = r"[a-z][a-z0-9_-]{0,63}"
"""Declared names: plugin IDs, plugin-local tool IDs, SubAgent names, entry, A2A agent IDs."""

OPAQUE_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
"""Caller-chosen IDs and labels: user, thread and workspace names."""


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
        Annotated[str, Field(pattern=f"^{NAME_PATTERN}$")],
        Annotated[StdioMCPServer | RemoteMCPServer, Field(discriminator="type")],
    ]

    @model_validator(mode="before")
    @classmethod
    def default_stdio(cls, value):
        if isinstance(value, Mapping) and isinstance(value.get("mcpServers"), Mapping):
            return {
                **value,
                "mcpServers": {
                    name: {"type": "stdio", **spec} if isinstance(spec, Mapping) else spec
                    for name, spec in value["mcpServers"].items()
                },
            }
        return value


class ToolSpec(StrictModel):
    """A Python function or native tool reference, not a second tool argument schema."""

    entrypoint: str = Field(min_length=1)
    scope: Literal["all", "subagents"] = "all"


def json_containers(value):
    """Convert frozen configuration-layer containers, without accepting arbitrary objects."""
    if isinstance(value, Mapping):
        return {key: json_containers(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_containers(item) for item in value]
    return value


class ExtensionConfig(StrictModel):
    params: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("params", mode="before")
    @classmethod
    def plain_params(cls, value):
        return json_containers(value)


HookEvent = Literal[
    "before_agent",
    "after_agent",
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
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
                    raise ValueError(
                        f"Hook group {event} already declares the event; remove inner event"
                    )
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
    """One SubAgent declaration: a standard prompt-driven agent or a compiled graph factory.

    ``kind="compiled"`` declares a plugin-provided graph factory: ``entrypoint``
    names a ``file.py:symbol`` whose callable receives the plugin's parameters
    and host services and returns a langchain Runnable (or a mapping with
    ``runnable`` and an optional ``description`` supplement). Compiled declarations
    take no ``system_prompt``, ``tools``, ``skills`` or ``hooks`` — the returned
    graph owns its prompts and execution. Declarations without ``kind`` keep the
    existing standard meaning.
    """

    schema_version: Literal[1] = 1
    kind: Literal["standard", "compiled"] = "standard"
    name: str = Field(pattern=f"^{NAME_PATTERN}$")
    description: str | None = Field(default=None, min_length=1)
    # A file relative to the plugin root; collect_subagents reads it into description.
    description_file: str | None = Field(default=None, min_length=1)
    # Prompt file relative to the plugin root.
    system_prompt_file: str | None = Field(default=None, min_length=1)
    entrypoint: str | None = Field(default=None, min_length=1)
    tools: list[str] | None = None
    # Omitted sources inherit the root's compiled Skills; [] explicitly opts out.
    skills: list[str] | None = None
    hooks: list[HookSpec] = Field(default_factory=list)
    filesystem_tools: (
        list[
            Literal[
                "ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"
            ]
        ]
        | None
    ) = None

    @model_validator(mode="before")
    @classmethod
    def normalize_hook_groups(cls, value):
        if isinstance(value, Mapping) and "hooks" in value:
            return {**value, "hooks": normalize_hooks(value["hooks"])}
        return value

    @model_validator(mode="after")
    def check_kind(self):
        if (self.description is None) == (self.description_file is None):
            raise ValueError(
                f"SubAgent {self.name!r} requires exactly one of description or description_file"
            )
        if self.kind == "compiled":
            if self.entrypoint is None:
                raise ValueError(
                    f"Compiled SubAgent {self.name!r} requires an entrypoint (file.py:symbol)"
                )
            if self.system_prompt_file is not None:
                raise ValueError(
                    f"Compiled SubAgent {self.name!r} does not take system_prompt_file; "
                    "the graph owns its prompts"
                )
            if (
                self.tools is not None
                or self.skills is not None
                or self.hooks
                or self.filesystem_tools is not None
            ):
                raise ValueError(
                    f"Compiled SubAgent {self.name!r} does not take tools, skills, hooks or filesystem_tools; "
                    "the graph owns its execution"
                )
        else:
            if self.filesystem_tools is not None and "read_file" not in self.filesystem_tools:
                raise ValueError("filesystem_tools must include read_file for large tool results")
            if self.system_prompt_file is None:
                raise ValueError(
                    f"SubAgent {self.name!r} requires system_prompt_file; "
                    "declare kind='compiled' for a graph factory instead"
                )
            if self.entrypoint is not None:
                raise ValueError(
                    f"SubAgent {self.name!r} uses entrypoint only with kind='compiled'"
                )
        return self


class RunHookSpec(ExtensionConfig):
    """A source-bound transformation at a compiled workflow boundary."""

    event: Literal["before_run", "after_run"]
    entrypoint: str = Field(min_length=1)


class PluginSpec(StrictModel):
    schema_version: Literal[3] = 3
    id: str = Field(pattern=f"^{NAME_PATTERN}$")
    requires: list[Annotated[str, Field(pattern=f"^{NAME_PATTERN}$")]] = Field(default_factory=list)
    # Files relative to the plugin root: plugins.config defaults and the prompt.
    defaults_file: str | None = Field(default=None, min_length=1)
    system_prompt_file: str | None = Field(default=None, min_length=1)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    subagents: list[str] = Field(default_factory=list)
    hooks: list[HookSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_declared_hooks(cls, value):
        if isinstance(value, Mapping) and "hooks" in value:
            return {**value, "hooks": normalize_hooks(value["hooks"])}
        return value

    @model_validator(mode="after")
    def reserved_id(self):
        if self.id in RESERVED_PLUGIN_IDS:
            raise ValueError(f"Reserved plugin ID: {self.id}")
        return self
