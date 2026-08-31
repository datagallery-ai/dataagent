import type { RuntimeContextSource } from "./runtime-context-source.js";

export class RuntimeContextSourceRegistry {
  private readonly sources = new Map<string, RuntimeContextSource>();

  register(source: RuntimeContextSource): void {
    if (this.sources.has(source.sourceType)) {
      throw new Error(`RUNTIME_CONTEXT_SOURCE_ALREADY_REGISTERED:${source.sourceType}`);
    }
    this.sources.set(source.sourceType, source);
  }

  list(): RuntimeContextSource[] {
    return [...this.sources.values()];
  }
}
