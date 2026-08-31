import type { ToolObservationAdapter } from "./tool-observation-adapter.js";

export class ToolObservationAdapterRegistry {
  private readonly adapters = new Map<string, ToolObservationAdapter>();

  register(adapter: ToolObservationAdapter): void {
    if (this.adapters.has(adapter.toolName)) {
      throw new Error(`TOOL_OBSERVATION_ADAPTER_ALREADY_REGISTERED:${adapter.toolName}`);
    }
    this.adapters.set(adapter.toolName, adapter);
  }

  resolveByToolName(toolName: string): ToolObservationAdapter | undefined {
    return this.adapters.get(toolName);
  }
}
