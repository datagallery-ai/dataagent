import { describe, expect, it } from "vitest";
import {
  mergeItemFromDto,
  workspaceConfigDtoToStore,
} from "../../../lib/config-api/adapter";

describe("config api adapter revision and secrets", () => {
  it("does not backfill credential fields from dto", () => {
    const store = workspaceConfigDtoToStore({
      datasources: [],
      knowledgeBases: [],
      mcpServers: [],
      modelProfiles: [
        {
          id: "deepseek",
          name: "DeepSeek",
          provider: "openai-compatible",
          modelName: "deepseek-chat",
          hasSecret: true,
          defaultEnabled: true,
          revision: 3,
        },
      ],
      skills: [],
    });

    expect(store.llm[0]?.hasSecret).toBe(true);
    expect(store.llm[0]?.settings?.apiKey).toBe("");
    expect(store.llm[0]?.revision).toBe(3);
  });

  it("preserves in-flight credential edits when merging dto", () => {
    const current = {
      id: "deepseek",
      name: "DeepSeek",
      description: "",
      enabled: true,
      revision: 3,
      settings: { apiKey: "sk-new" },
    };
    const merged = mergeItemFromDto("llm", current, {
      id: "deepseek",
      name: "DeepSeek",
      provider: "openai-compatible",
      modelName: "deepseek-chat",
      hasSecret: true,
      revision: 4,
    });

    expect(merged.revision).toBe(4);
    expect(merged.settings?.apiKey).toBe("sk-new");
  });

  it("drops MCP credential plaintext after the backend confirms a save", () => {
    const current = {
      id: "custom-auth",
      name: "Custom auth",
      description: "",
      enabled: true,
      settings: {
        authType: "custom-header",
        customHeaderName: "X-API-Key",
        customHeaderValue: "header-secret",
      },
    };
    const merged = mergeItemFromDto("mcp", current, {
      id: "custom-auth",
      name: "Custom auth",
      authType: "custom-header",
      hasSecret: true,
      revision: 2,
    });

    expect(merged.hasSecret).toBe(true);
    expect(merged.persistedAuthType).toBe("custom-header");
    expect(merged.settings?.customHeaderName).toBe("");
    expect(merged.settings?.customHeaderValue).toBe("");
  });
});
