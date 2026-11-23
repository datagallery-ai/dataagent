import { describe, expect, it } from "vitest";
import {
  applyBackendCapabilities,
  getRuntimeCapabilities,
  resetCapabilitiesForTests,
} from "../../../lib/config-api/capabilities";
import { shouldRestoreConversation } from "../conversation-restore";
import {
  hasCapability,
  hasPendingCapability,
  isSelectOptionPending,
  setLiveDatasourceTypes,
  setLivePendingCapabilities,
  WORKSPACE_CONFIG_FIELDS,
} from "../data-task-state";

describe("chat attachment capabilities", () => {
  it("defaults chat.imageInput and chat.fileUpload to false", () => {
    resetCapabilitiesForTests();
    const mapped = applyBackendCapabilities({});
    expect(mapped["chat.imageInput"]).toBe(false);
    expect(mapped["chat.fileUpload"]).toBe(false);
    expect(mapped["conversation.title"]).toBe(false);
    expect(mapped.files).toBe(false);
  });

  it("maps backend response flags through", () => {
    const mapped = applyBackendCapabilities({
      "artifact.list": true,
      "artifact.promote": true,
      "chat.imageInput": true,
      "chat.fileUpload": true,
      files: true,
      "conversation.memory": true,
      "conversation.title": true,
    });
    expect(mapped["chat.imageInput"]).toBe(true);
    expect(mapped["chat.fileUpload"]).toBe(true);
    expect(mapped["artifact.list"]).toBe(true);
    expect(mapped["artifact.promote"]).toBe(true);
    expect(mapped.files).toBe(true);
    expect(mapped["conversation.title"]).toBe(true);
    expect(getRuntimeCapabilities().conversationMemory).toBe(true);
    expect(
      shouldRestoreConversation({
        conversationMemoryEnabled: getRuntimeCapabilities().conversationMemory,
        messageCount: 0,
        isRunning: false,
        alreadyRestored: false,
      }),
    ).toBe(true);
  });

  it("updates pending capability flags from backend response mappings", () => {
    setLivePendingCapabilities({
      "datasource.introspectionPolicy": true,
      "kb.chunking": true,
      "kb.citationPolicy": true,
      "kb.scope": true,
      "mcp.stdio": true,
      "skill.resourceBinding": true,
    });
    expect(hasPendingCapability("datasource.introspectionPolicy")).toBe(true);
    expect(hasPendingCapability("kb.chunking")).toBe(true);
    expect(hasPendingCapability("kb.citationPolicy")).toBe(true);
    expect(hasPendingCapability("kb.scope")).toBe(true);
    expect(hasPendingCapability("mcp.stdio")).toBe(true);
    expect(hasPendingCapability("skill.resourceBinding")).toBe(true);
    setLivePendingCapabilities({
      "datasource.introspectionPolicy": false,
      "kb.chunking": false,
      "kb.citationPolicy": false,
      "kb.scope": false,
      "mcp.stdio": false,
      "skill.resourceBinding": false,
    });
  });

  it("unlocks only datasource types reported as enabled by the backend", () => {
    setLiveDatasourceTypes([
      { name: "duckdb", label: "DuckDB", enabled: true },
      { name: "postgresql", label: "PostgreSQL", enabled: true },
      { name: "clickhouse", label: "ClickHouse", enabled: true },
      { name: "oracle", label: "Oracle", enabled: false },
    ]);
    const typeField = WORKSPACE_CONFIG_FIELDS.db.find((field) => field.key === "type");
    const options = typeField?.getOptions?.({}) ?? [];
    expect(options.find((option) => option.value === "clickhouse")?.label).toBe("ClickHouse");
    expect(options.find((option) => option.value === "oracle")?.label).toBe("Oracle");
    expect(typeField ? isSelectOptionPending(typeField, "clickhouse") : true).toBe(false);
    expect(typeField ? isSelectOptionPending(typeField, "oracle") : false).toBe(true);
  });
});
