import type { DataGateway } from "@datafoundry/data-gateway";
import { describe, expect, it } from "vitest";

import { createDataFoundryToolRegistry } from "./data-tools.js";

describe("data tool SQL reuse", () => {
  it("reuses an exact successful SQL result within one run and schema", async () => {
    let executionCount = 0;
    const dataGateway = {
      inspectSchema: async () => ({ datasource_id: "orders", dialect: "sqlite", tables: [] }),
      runSqlReadonly: async () => {
        executionCount += 1;
        return {
          columns: ["value"],
          rows: [[1]],
          row_count: 1,
          audit_log_id: "audit-1",
          artifact_id: "artifact-1",
          elapsed_ms: 1
        };
      }
    } as unknown as DataGateway;
    const registry = createDataFoundryToolRegistry({
      dataGateway,
      emitter: { emit: () => undefined },
      runContext: {
        user_id: "user-1",
        workspace_id: "workspace-1",
        session_id: "session-1",
        run_id: "run-cache",
        user_input: "analyze orders",
        chat_mode: "copilotkit",
        enabled_datasource_ids: ["orders"],
        selected_datasource_id: "orders",
        model_name: "test-model"
      }
    });
    const schema = await registry.inspectSchema({ datasource_id: "orders" });

    const first = await registry.runSqlReadonly({ schema_id: schema.schema_id, sql: "SELECT 1", limit: 10 });
    const second = await registry.runSqlReadonly({ schema_id: schema.schema_id, sql: "SELECT 1", limit: 10 });
    await registry.runSqlReadonly({ schema_id: schema.schema_id, sql: "SELECT 1", limit: 20 });

    expect(first.cache_hit).toBeUndefined();
    expect(second).toMatchObject({ cache_hit: true, result: { audit_log_id: "audit-1" } });
    expect(executionCount).toBe(2);
    expect(registry.state.sql_execution_count).toBe(2);
  });
});
