import { describe, expect, it } from "vitest";
import {
  defaultWorkspaceConfig,
  getEnabledLlmItems,
  resolveActiveLlmProfileId,
  summarizeConfigItems,
  summarizeLlmItems,
  summarizeMcpItems,
  type WorkspaceConfigStore,
} from "../data-task-state";

describe("workspace config defaults", () => {
  it("starts with empty lists instead of hardcoded builtin demo/server-default", () => {
    expect(defaultWorkspaceConfig()).toEqual({
      db: [],
      kb: [],
      mcp: [],
      llm: [],
      skill: [],
    });
  });

  it("summarizes configured items as default available even if legacy enabled is false", () => {
    const item = {
      id: "db-1",
      name: "Orders DB",
      description: "Read-only warehouse",
      enabled: false,
    };

    expect(summarizeConfigItems([item], "未配置")).toBe("Orders DB");
    expect(summarizeMcpItems([item], "未配置")).toBe("Orders DB");
  });

  it("keeps llm profiles available even if legacy enabled is false", () => {
    const workspaceConfig: WorkspaceConfigStore = {
      db: [],
      kb: [],
      mcp: [],
      skill: [],
      llm: [
        {
          id: "server-default",
          name: "default",
          description: "Server env",
          enabled: false,
          settings: { modelName: "qwen-plus" },
        },
      ],
    };

    expect(getEnabledLlmItems(workspaceConfig).map((item) => item.id)).toEqual([
      "server-default",
    ]);
    expect(summarizeLlmItems(workspaceConfig.llm, "未配置")).toBe("default");
  });
});

describe("active LLM selection", () => {
  it("keeps a valid dialog selection instead of restoring the workspace default", () => {
    const profiles = [
      { id: "server-default", name: "default", description: "", enabled: true },
      { id: "profile-b", name: "Profile B", description: "", enabled: true },
    ];

    expect(
      resolveActiveLlmProfileId(profiles, "profile-b", "server-default"),
    ).toBe("profile-b");
  });

  it("returns null when no profiles are available", () => {
    expect(resolveActiveLlmProfileId([], null, "server-default")).toBeNull();
    expect(resolveActiveLlmProfileId([], "stale-id", null)).toBeNull();
  });

  it("switches away from a failed active profile to a connected one", () => {
    const profiles = [
      {
        id: "glm-failed",
        name: "GLM-5.2",
        description: "",
        enabled: true,
        status: "failed" as const,
      },
      {
        id: "deepseek-ok",
        name: "deepseek-v4-pro",
        description: "",
        enabled: true,
        status: "connected" as const,
      },
    ];

    expect(
      resolveActiveLlmProfileId(profiles, "glm-failed", "glm-failed"),
    ).toBe("deepseek-ok");
  });

  it("prefers a connected fallback over an untested first profile", () => {
    const profiles = [
      {
        id: "untested",
        name: "qwen",
        description: "",
        enabled: true,
        status: "untested" as const,
      },
      {
        id: "connected",
        name: "deepseek",
        description: "",
        enabled: true,
        status: "connected" as const,
      },
    ];

    expect(resolveActiveLlmProfileId(profiles, null, "untested")).toBe(
      "connected",
    );
  });

  it("keeps a failed selection when no connected profile exists", () => {
    const profiles = [
      {
        id: "glm-failed",
        name: "GLM-5.2",
        description: "",
        enabled: true,
        status: "failed" as const,
      },
    ];

    expect(
      resolveActiveLlmProfileId(profiles, "glm-failed", null),
    ).toBe("glm-failed");
  });
});
