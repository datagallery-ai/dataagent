import { describe, expect, it } from "vitest";
import { z } from "zod";

import { CapabilityRegistry } from "./capability-registry.js";
import type { CapabilityPlugin } from "./types.js";

describe("CapabilityRegistry", () => {
  it("rejects action namespace conflicts across plugins", () => {
    const registry = new CapabilityRegistry();
    registry.register(createPlugin("plugin-a", "data.schema.inspect"));

    expect(() => registry.register(createPlugin("plugin-b", "data.schema.inspect")))
      .toThrow("CAPABILITY_ACTION_ALREADY_REGISTERED:data.schema.inspect:plugin-a:plugin-b");
  });

  it("rejects a plugin whose required dependency is missing", () => {
    const registry = new CapabilityRegistry();
    const plugin = createPlugin("plugin-b", "data.query.execute");
    plugin.manifest.requires = [{ id: "plugin-a", version: "1" }];

    expect(() => registry.register(plugin))
      .toThrow("CAPABILITY_PLUGIN_DEPENDENCY_MISSING:plugin-b:plugin-a@1");
  });

  it("initializes plugins in registration order and disposes them in reverse order", async () => {
    const lifecycle: string[] = [];
    const registry = new CapabilityRegistry();
    const first = createPlugin("plugin-a", "data.schema.inspect");
    first.initialize = async () => { lifecycle.push("init:a"); };
    first.dispose = async () => { lifecycle.push("dispose:a"); };
    const second = createPlugin("plugin-b", "data.query.execute");
    second.initialize = async () => { lifecycle.push("init:b"); };
    second.dispose = async () => { lifecycle.push("dispose:b"); };
    registry.register(first);
    registry.register(second);

    await registry.initialize();
    await registry.dispose();

    expect(lifecycle).toEqual(["init:a", "init:b", "dispose:b", "dispose:a"]);
  });
});

const createPlugin = (pluginId: string, actionName: string): CapabilityPlugin => ({
  manifest: { id: pluginId, version: "1", provides: [actionName] },
  actions: [{
    name: actionName,
    exposure: "agent",
    inputSchema: z.object({}),
    outputSchema: z.object({ ok: z.boolean() }),
    idempotency: "supported",
    execute: async () => ({ ok: true })
  }]
});
