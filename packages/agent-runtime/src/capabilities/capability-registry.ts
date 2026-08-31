import type {
  CapabilityActionDefinition,
  CapabilityExposure,
  CapabilityPlugin,
  RegisteredCapabilityAction
} from "./types.js";

export class CapabilityRegistry {
  private readonly actions = new Map<string, RegisteredCapabilityAction>();
  private readonly plugins = new Map<string, CapabilityPlugin>();
  private initializedPlugins: CapabilityPlugin[] = [];

  register(plugin: CapabilityPlugin): void {
    for (const dependency of plugin.manifest.requires ?? []) {
      const registered = this.plugins.get(dependency.id);
      if (!registered || (dependency.version && registered.manifest.version !== dependency.version)) {
        throw new Error(
          `CAPABILITY_PLUGIN_DEPENDENCY_MISSING:${plugin.manifest.id}:${dependency.id}@`
          + (dependency.version ?? "any")
        );
      }
    }
    for (const action of plugin.actions) {
      const existing = this.actions.get(action.name);
      if (existing) {
        throw new Error(
          `CAPABILITY_ACTION_ALREADY_REGISTERED:${action.name}:${existing.pluginId}:${plugin.manifest.id}`
        );
      }
    }
    this.plugins.set(plugin.manifest.id, plugin);
    for (const action of plugin.actions) {
      this.actions.set(action.name, {
        action,
        pluginId: plugin.manifest.id,
        pluginVersion: plugin.manifest.version
      });
    }
  }

  resolve(actionName: string): RegisteredCapabilityAction | undefined {
    return this.actions.get(actionName);
  }

  listByExposure(exposure: CapabilityExposure): CapabilityActionDefinition[] {
    return [...this.actions.values()]
      .map((entry) => entry.action)
      .filter((action) => action.exposure === exposure || action.exposure === "both");
  }

  async initialize(): Promise<void> {
    if (this.initializedPlugins.length > 0) {
      return;
    }
    try {
      for (const plugin of this.plugins.values()) {
        await plugin.initialize?.();
        this.initializedPlugins.push(plugin);
      }
    } catch (error) {
      await this.dispose();
      throw error;
    }
  }

  async dispose(): Promise<void> {
    const failures: unknown[] = [];
    for (const plugin of [...this.initializedPlugins].reverse()) {
      try {
        await plugin.dispose?.();
      } catch (error) {
        failures.push(error);
      }
    }
    this.initializedPlugins = [];
    if (failures.length > 0) {
      throw new AggregateError(failures, "CAPABILITY_PLUGIN_DISPOSE_FAILED");
    }
  }
}
