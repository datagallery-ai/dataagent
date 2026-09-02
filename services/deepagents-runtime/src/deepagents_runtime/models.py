from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RuntimeModelRef(BaseModel):
    name: str | None = None
    profileId: str | None = None
    provider: str | None = None


class RuntimeLimits(BaseModel):
    maxSteps: int | None = None


class RuntimeInterrupt(BaseModel):
    type: str
    args: Any = None
    resumeSchema: Any = None
    runId: str
    suspendPayload: Any = None
    toolCallId: str
    toolName: str


class RuntimeRunResume(BaseModel):
    interrupt: RuntimeInterrupt
    response: Any = None


class RuntimeTrace(BaseModel):
    userId: str | None = None
    workspaceId: str | None = None


class RuntimeRunRequest(BaseModel):
    checkpointRef: str | None = None
    limits: RuntimeLimits | None = None
    messages: list[dict[str, Any]]
    model: RuntimeModelRef | None = None
    resume: RuntimeRunResume | None = None
    runId: str
    systemPrompt: str = ""
    threadId: str
    trace: RuntimeTrace | None = None


class CancelRequest(BaseModel):
    reason: str = "RUN_CANCELLED"


class RuntimeCapabilities(BaseModel):
    cancel: bool = True
    interrupt: bool = True
    streaming: bool = True
    tools: bool = True


class RuntimeHealth(BaseModel):
    capabilities: RuntimeCapabilities = Field(default_factory=RuntimeCapabilities)
    provider: str
    status: str
    version: str
