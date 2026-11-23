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
});
