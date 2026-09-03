from __future__ import annotations

from typing import Any

CAPABILITIES: dict[str, bool] = {
    "artifact.export": False,
    "artifact.list": False,
    "artifact.promote": False,
    "chat.fileUpload": False,
    "chat.imageInput": False,
    "conversation.memory": False,
    "conversation.title": False,
    "interaction.resume": False,
    "runtime.dataTools": False,
    "runtime.traceDag": False,
    "datasource.fieldMasking": False,
    "datasource.extendedTypes": False,
    "datasource.introspectionPolicy": False,
    "datasource.queryPolicy": False,
    "datasource.samplePolicy": False,
    "datasource.server": False,
    "files": False,
    "kb.chunking": False,
    "kb.citationPolicy": False,
    "kb.scope": False,
    "llm.advancedSampling": False,
    "llm.samplingParams": True,
    "knowledge": False,
    "mcp": False,
    "mcp.stdio": False,
    "mcp.toolPolicy": False,
    "skill.resourceBinding": False,
    "skills": False,
}

WORKSPACE_CONFIG: dict[str, list[Any]] = {
    "datasources": [],
    "knowledgeBases": [],
    "mcpServers": [],
    "modelProfiles": [],
    "skills": [],
}

RUN_DEFAULTS: dict[str, Any] = {
    "enabledDatasourceIds": [],
    "enabledKnowledgeIds": [],
    "enabledMcpServerIds": [],
    "enabledSkillIds": [],
    "activeLlmProfileId": "server-default",
    "activeSkillId": "",
}
