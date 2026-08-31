import { join } from "node:path";

const DEMO_DUCKDB_FILENAME = "api-duckdb-demo.duckdb";

export const demoDuckDbPath = (): string => join(process.cwd(), "storage", "demo", DEMO_DUCKDB_FILENAME);

export const createDemoDuckDbConfig = (): Record<string, unknown> => ({
  builtin: true,
  defaultEnabled: true,
  mode: "demo",
  path: demoDuckDbPath()
});
