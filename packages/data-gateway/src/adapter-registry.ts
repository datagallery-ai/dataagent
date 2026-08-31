import type { DataSourceAdapter, DataSourceType } from "./types.js";

export type AdapterFactory = (config: Record<string, unknown>) => DataSourceAdapter;

export type AdapterRegistry = ReadonlyMap<string, AdapterFactory>;

export const createAdapterRegistry = (factories: Record<string, AdapterFactory>): AdapterRegistry =>
  new Map(Object.entries(factories));

export const createRegisteredAdapter = (
  registry: AdapterRegistry,
  type: DataSourceType,
  config: Record<string, unknown>
): DataSourceAdapter => {
  const factory = registry.get(type);

  if (!factory) {
    throw new Error(`Unsupported data source type: ${type}`);
  }

  return factory(config);
};
