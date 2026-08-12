import { describe, expect, it } from "vitest";

import type { SemanticRequest, SemanticResolution } from "../semantic/types.js";
import { ToolExecutionError } from "../errors/tool-execution-error.js";
import { createUserAnalysisRequirements } from "./analysis-requirements.js";
import { createRunProtocolBoundary } from "./run-protocol-boundary.js";
import { InMemoryProtocolStateStore } from "./in-memory-protocol-state-store.js";
import type { DataAnalysisState } from "./protocols/data-analysis.js";
import type { ProtocolEvent } from "./types.js";

describe("createRunProtocolBoundary", () => {
  it("extracts user requirements before starting data-analysis and skips extraction on restore", async () => {
    const stateStore = new InMemoryProtocolStateStore();
    let extractionCount = 0;
    const firstEvents: ProtocolEvent[] = [];
    const first = await createRunProtocolBoundary({
      runId: "run-requirement-extraction",
      userInput: "分析并核对利润公式",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-requirements", revision: 0 },
      tools: {},
      stateStore,
      requirementExtractor: async () => {
        extractionCount += 1;
        return createUserAnalysisRequirements([
          { kind: "data_quality", description: "核对利润公式", acceptanceCriteria: ["报告错误数"] }
        ]);
      },
      projectContext: () => ({ packageId: "context-requirements", revision: 0 }),
      runtimeOptions: { onEvent: (event) => firstEvents.push(event) }
    });

    expect(first.protocolRuntime.getState("run-requirement-extraction").domain).toMatchObject({
      requirements: expect.arrayContaining([expect.objectContaining({ id: "R1", description: "核对利润公式" })])
    });
    await first.dispose();

    const restoredEvents: ProtocolEvent[] = [];
    const restored = await createRunProtocolBoundary({
      runId: "run-requirement-extraction",
      userInput: "继续分析",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "ignored", revision: 0 },
      tools: {},
      stateStore,
      requirementExtractor: async () => {
        extractionCount += 1;
        throw new Error("extractor must not run during restore");
      },
      projectContext: () => ({ packageId: "context-requirements", revision: 0 }),
      runtimeOptions: { onEvent: (event) => restoredEvents.push(event) }
    });

    expect(extractionCount).toBe(1);
    expect(firstEvents.length).toBeGreaterThan(0);
    expect(restoredEvents).toEqual([]);
    expect(restored.protocolRuntime.getState("run-requirement-extraction").domain).toMatchObject({
      requirements: expect.arrayContaining([expect.objectContaining({ id: "R1" })])
    });
  });

  it("binds SQL evidence and committed claims to extracted requirements", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-requirement-evidence",
      userInput: "分析并计算新增利润",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-requirement-evidence", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-profit",
            audit_log_id: "audit-profit",
            columns: ["incremental_profit"],
            rows: [[7100.6]],
            row_count: 1
          }
        }) }
      },
      requirementExtractor: async () => createUserAnalysisRequirements([
        { kind: "metric", description: "计算新增利润", acceptanceCriteria: ["精确到分"] }
      ]),
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-requirement-evidence", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-requirement-evidence",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: {}
    });
    await boundary.actionRouter.execute({
      runId: "run-requirement-evidence",
      segmentId: boundary.segmentId,
      actionId: "sql-1",
      actionName: "run_sql_readonly",
      input: {
        schema_id: "schema-1",
        sql: "select 7100.6 as incremental_profit",
        requirement_ids: ["R1"],
        expected_columns: ["incremental_profit"]
      }
    });
    let state = boundary.protocolRuntime.getState("run-requirement-evidence");
    expect((state.domain as { requirements: Array<{ id: string; status: string }> }).requirements).toEqual(
      expect.arrayContaining([expect.objectContaining({ id: "R1", status: "evidenced" })])
    );

    await boundary.actionRouter.execute({
      runId: "run-requirement-evidence",
      segmentId: boundary.segmentId,
      actionId: "commit-1",
      actionName: "analysis.requirements.commit",
      input: {
        claims: [{
          requirement_id: "R1",
          claim: "新增利润为 7100.60 元",
          evidence_refs: ["artifact-profit"]
        }]
      }
    });
    state = boundary.protocolRuntime.getState("run-requirement-evidence");
    const terminal = boundary.protocolRuntime.proposeCompletion({
      runId: "run-requirement-evidence",
      segmentId: boundary.segmentId,
      expectedRevision: state.revision
    });
    expect(terminal.terminalDecision?.status).toBe("completed");
  });

  it("blocks evidence when a structured result invariant fails", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-result-invariant-failure",
      userInput: "分析订单转化率并核对分子分母",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-result-invariant", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1", dialect: "sqlite" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-ratio",
            audit_log_id: "audit-ratio",
            columns: ["converted", "total", "conversion_rate"],
            rows: [[40, 100, 0.5]],
            row_count: 1
          }
        }) }
      },
      requirementExtractor: async () => createUserAnalysisRequirements([{
        kind: "metric",
        description: "计算订单转化率",
        acceptanceCriteria: ["转化率等于转化订单数除以总订单数"],
        assertions: [{
          kind: "metric",
          description: "订单转化率必须可由分子分母复算",
          resultChecks: [{
            kind: "ratio",
            required: true,
            value: { field: "conversion_rate" },
            numerator: { field: "converted" },
            denominator: { field: "total" },
            tolerance: 0.000001
          }],
          claimValues: [{
            name: "conversion_rate",
            field: "conversion_rate",
            required: true,
            tolerance: 0.000001
          }]
        }]
      }]),
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-result-invariant", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-result-invariant-failure",
      segmentId: boundary.segmentId,
      actionId: "inspect-result-invariant",
      actionName: "inspect_schema",
      input: {}
    });
    await boundary.actionRouter.execute({
      runId: "run-result-invariant-failure",
      segmentId: boundary.segmentId,
      actionId: "sql-result-invariant",
      actionName: "run_sql_readonly",
      input: {
        schema_id: "schema-1",
        sql: "select 40 as converted, 100 as total, 0.5 as conversion_rate",
        requirement_ids: ["R1"],
        assertion_ids: ["R1.A1"],
        expected_columns: ["converted", "total", "conversion_rate"]
      }
    });

    const state = boundary.protocolRuntime.getState("run-result-invariant-failure").domain as DataAnalysisState;
    expect(state.validationPassed).toBe(false);
    expect(state.evidenceBindings).toEqual([]);
    expect(state.queryAttempts[0]?.resultValidationFindings).toEqual([
      expect.objectContaining({ code: "RESULT_CHECK_RATIO_FAILED", severity: "error" })
    ]);
    expect(state.queryAttempts[0]?.verifiedValues).toEqual([]);
    expect(state.requirements).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "R1", status: "queried" })
    ]));
  });

  it("routes an analytic request to data-analysis and governs every selected tool", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-1",
      userInput: "按月分析销售额",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-1", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-1",
            audit_log_id: "audit-1",
            columns: ["value"],
            rows: [[1]],
            row_count: 1
          }
        }) }
      },
      semanticProvider: {
        resolve: async (request) => ({
          value: { nodes: [] },
          capabilities: ["graph-explore"],
          trust: "verified",
          warnings: [],
          provider: "datalink",
          mode: "live",
          datasourceRevision: request.datasourceRevision
        })
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      projectContext: ({ actionName }) => ({
        contextPackageRef: {
          packageId: "context-1",
          revision: actionName === "inspect_schema" ? 1 : 2
        }
      })
    });

    expect(boundary.route.definition.id).toBe("data-analysis");
    expect(boundary.capabilityRegistry.resolve("inspect_schema")).toBeDefined();
    expect(boundary.capabilityRegistry.resolve("run_sql_readonly")).toBeDefined();

    await boundary.actionRouter.execute({
      runId: "run-1",
      segmentId: boundary.segmentId,
      actionId: "action-1",
      actionName: "inspect_schema",
      input: {}
    });
    expect(boundary.protocolRuntime.getState("run-1").phase).toBe("query_planning");

    await boundary.actionRouter.execute({
      runId: "run-1",
      segmentId: boundary.segmentId,
      actionId: "action-2",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "select 1" }
    });
    const state = boundary.protocolRuntime.getState("run-1");
    expect(state.phase).toBe("synthesis");
    expect(state.actions.map((action) => action.actionName)).toEqual([
      "inspect_schema",
      "semantic.context.resolve",
      "data.query.plan",
      "data.query.validate",
      "run_sql_readonly",
      "analysis.result.validate",
      "analysis.evidence.bind"
    ]);
    const terminal = boundary.protocolRuntime.proposeCompletion({
      runId: "run-1",
      segmentId: boundary.segmentId,
      expectedRevision: state.revision
    });
    expect(terminal.terminalDecision?.status).toBe("completed");
  });

  it("resolves semantic context through the configured provider before SQL execution", async () => {
    const requests: unknown[] = [];
    const events: ProtocolEvent[] = [];
    const boundary = await createRunProtocolBoundary({
      runId: "run-semantic",
      userInput: "分析订单销售额",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-semantic", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-1",
            audit_log_id: "audit-1",
            columns: ["value"],
            rows: [[1]],
            row_count: 1
          }
        }) }
      },
      semanticProvider: {
        resolve: async (request) => {
          requests.push(request);
          return {
            value: { nodes: [] },
            capabilities: ["graph-explore"],
            trust: "verified",
            warnings: [],
            provider: "datalink",
            mode: "live",
            datasourceRevision: request.datasourceRevision
          };
        }
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      runtimeOptions: { onEvent: (event) => events.push(event) },
      projectContext: () => ({ packageId: "context-semantic", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-semantic",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: { datasource_id: "orders-db" }
    });

    expect(requests).toEqual([{
      userId: "user-1",
      workspaceId: "workspace-1",
      datasourceId: "orders-db",
      datasourceRevision: "schema-v1",
      query: "分析订单销售额",
      physicalSchema: { schema_id: "schema-1" }
    }]);
    expect(boundary.protocolRuntime.getState("run-semantic").actions.map((action) => action.actionName)).toEqual([
      "inspect_schema",
      "semantic.context.resolve"
    ]);
    expect(boundary.protocolRuntime.getState("run-semantic").phase).toBe("query_planning");
    expect(events).toContainEqual(expect.objectContaining({
      type: "protocol.action.succeeded",
      payload: {
        actionId: "inspect-1:auto:1",
        actionName: "semantic.context.resolve",
        result: {
          provider: "datalink",
          mode: "live",
          trust: "verified",
          datasourceRevision: "schema-v1"
        }
      }
    }));
  });

  it("grounds logical requirements after schema and semantic resolution", async () => {
    const groundingInputs: unknown[] = [];
    const events: ProtocolEvent[] = [];
    let schemaInspectionCount = 0;
    const boundary = await createRunProtocolBoundary({
      runId: "run-contract-grounding",
      userInput: "统计物流单量",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-contract-grounding", revision: 0 },
      tools: {
        inspect_schema: {
          execute: async () => ({
            schema_id: schemaInspectionCount++ === 0 ? "schema-shipments" : "schema-shipments-refresh",
            tables: [{ name: "dacomp-zh-006", columns: [{ name: "物流单号", type: "VARCHAR" }] }]
          })
        }
      },
      requirementExtractor: async () => createUserAnalysisRequirements([{
        kind: "metric",
        description: "统计物流单量",
        acceptanceCriteria: ["报告总物流单量"]
      }]),
      analysisContractGrounder: async (input) => {
        groundingInputs.push(input);
        return {
          requirements: input.requirements.map((requirement) => requirement.id === "R1"
            ? {
                ...requirement,
                assertions: [{
                  id: "R1.A1",
                  requirementId: "R1",
                  kind: "metric" as const,
                  description: "物流单量",
                  required: true,
                  sourceTables: ["dacomp-zh-006"],
                  dimensions: [],
                  sqlConstraints: [{
                    kind: "aggregate" as const,
                    function: "COUNT",
                    column: "物流单号",
                    alias: "shipment_count"
                  }],
                  resultChecks: [],
                  claimValues: [{ name: "shipment_count", field: "shipment_count", required: true }]
                }]
              }
            : requirement),
          findings: []
        };
      },
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-contract-grounding", revision: 1 }),
      runtimeOptions: { onEvent: (event) => events.push(event) }
    });

    const result = await boundary.actionRouter.execute({
      runId: "run-contract-grounding",
      segmentId: boundary.segmentId,
      actionId: "inspect-contract-grounding",
      actionName: "inspect_schema",
      input: {}
    });

    expect(groundingInputs).toEqual([expect.objectContaining({
      datasourceRevision: "schema-v1",
      physicalSchema: expect.objectContaining({ schema_id: "schema-shipments" }),
      semanticResolution: expect.objectContaining({ provider: "datalink", mode: "live" })
    })]);
    const state = boundary.protocolRuntime.getState("run-contract-grounding");
    expect(state.phase).toBe("query_planning");
    expect(state.actions.map((action) => action.actionName)).toEqual([
      "inspect_schema",
      "semantic.context.resolve",
      "analysis.contract.ground"
    ]);
    expect(state.domain).toMatchObject({
      contractGrounded: true,
      contractDatasourceRevision: "schema-v1",
      requirements: expect.arrayContaining([expect.objectContaining({
        id: "R1",
        assertions: [expect.objectContaining({
          id: "R1.A1",
          sourceTables: ["dacomp-zh-006"]
        })]
      })])
    });
    expect(events).toContainEqual(expect.objectContaining({
      type: "protocol.action.succeeded",
      payload: {
        actionId: "inspect-contract-grounding:auto:1:auto:1",
        actionName: "analysis.contract.ground",
        result: {
          datasourceRevision: "schema-v1",
          structuredRequirementIds: ["R1"],
          manualRequirementIds: [],
          findings: []
        }
      }
    }));
    expect(result.observation).toMatchObject({
      schema_id: "schema-shipments",
      analysis_contract: {
        requirements: [{
          requirement_id: "R1",
          assertions: [{
            assertion_id: "R1.A1",
            sql_constraints: [expect.objectContaining({
              kind: "aggregate",
              alias: "shipment_count"
            })],
            claim_values: [{ name: "shipment_count", field: "shipment_count", required: true }]
          }]
        }]
      }
    });

    await expect(boundary.actionRouter.execute({
      runId: "run-contract-grounding",
      segmentId: boundary.segmentId,
      actionId: "inspect-contract-grounding-again",
      actionName: "inspect_schema",
      input: {}
    })).resolves.toBeDefined();
    expect(groundingInputs).toHaveLength(1);
    expect(boundary.protocolRuntime.getState("run-contract-grounding").domain).toMatchObject({
      contractGrounded: true,
      contractDatasourceRevision: "schema-v1"
    });
  });

  it("restores the latest protocol segment after an accepted handoff", async () => {
    const stateStore = new InMemoryProtocolStateStore();
    const first = await createRunProtocolBoundary({
      runId: "run-handoff",
      userInput: "解释这个项目",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-handoff", revision: 0 },
      tools: {},
      stateStore,
      projectContext: () => ({ packageId: "context-handoff", revision: 0 })
    });
    first.handoffCoordinator.handoff({
      runId: "run-handoff",
      segmentId: first.segmentId,
      expectedRevision: 0,
      authorizedProtocolIds: ["general-task", "data-analysis"],
      target: { protocolId: "data-analysis", protocolVersion: "1" },
      reasonCodes: ["ANALYTIC_INTENT"],
      unresolvedGoals: []
    });

    const restored = await createRunProtocolBoundary({
      runId: "run-handoff",
      userInput: "继续",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "ignored", revision: 0 },
      tools: {},
      stateStore,
      projectContext: () => ({ packageId: "context-handoff", revision: 0 })
    });

    expect(restored.route.definition.id).toBe("data-analysis");
    expect(restored.segmentId).toBe("run-handoff:segment:2");
    expect(restored.protocolRuntime.getState("run-handoff").status).toBe("active");
  });

  it("rejects non-read-only SQL before the data executor runs", async () => {
    let sqlExecuted = false;
    const boundary = await createRunProtocolBoundary({
      runId: "run-invalid-sql",
      userInput: "分析并删除订单",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-invalid", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: {
          execute: async () => {
            sqlExecuted = true;
            return {};
          }
        }
      },
      semanticProvider: {
        resolve: async (request) => ({
          value: {},
          capabilities: ["graph-explore"],
          trust: "verified",
          warnings: [],
          provider: "datalink",
          mode: "live",
          datasourceRevision: request.datasourceRevision
        })
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      projectContext: () => ({ packageId: "context-invalid", revision: 1 })
    });
    await boundary.actionRouter.execute({
      runId: "run-invalid-sql",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: { datasource_id: "orders-db" }
    });

    await expect(boundary.actionRouter.execute({
      runId: "run-invalid-sql",
      segmentId: boundary.segmentId,
      actionId: "sql-1",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "DELETE FROM orders" }
    })).rejects.toThrow("QUERY_CONTRACT_VALIDATION_FAILED");
    expect(sqlExecuted).toBe(false);
  });

  it("accepts read-only SQL with leading comments", async () => {
    let sqlExecuted = false;
    const boundary = await createRunProtocolBoundary({
      runId: "run-commented-sql",
      userInput: "分析订单",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-commented", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: {
          execute: async () => {
            sqlExecuted = true;
            return {
              result: {
                artifact_id: "artifact-commented",
                audit_log_id: "audit-commented",
                columns: ["value"],
                rows: [[1]],
                row_count: 1
              }
            };
          }
        }
      },
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-commented", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-commented-sql",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: {}
    });
    await boundary.actionRouter.execute({
      runId: "run-commented-sql",
      segmentId: boundary.segmentId,
      actionId: "sql-1",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "-- explain this query\nSELECT 1" }
    });

    expect(sqlExecuted).toBe(true);
  });

  it("returns SQL contract findings before an invalid query reaches the executor", async () => {
    let sqlExecuted = false;
    const boundary = await createRunProtocolBoundary({
      runId: "run-query-contract-failure",
      userInput: "统计完整验证期订单数",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-query-contract", revision: 0 },
      tools: {
        inspect_schema: {
          execute: async () => ({
            schema_id: "schema-1",
            dialect: "sqlite",
            tables: [{
              name: "orders",
              columns: [{ name: "order_date", type: "DATE" }]
            }]
          })
        },
        run_sql_readonly: {
          execute: async () => {
            sqlExecuted = true;
            return { result: { rows: [], columns: [], row_count: 0 } };
          }
        }
      },
      requirementExtractor: async () => createUserAnalysisRequirements([{
        kind: "metric",
        description: "统计完整验证期订单数",
        acceptanceCriteria: ["包含12月31日"],
        assertions: [{
          kind: "metric",
          description: "验证期订单数",
          sourceTables: ["orders"],
          sqlConstraints: [{
            kind: "time_range",
            column: "order_date",
            start: "2023-07-01",
            end: "2023-12-31",
            endInclusive: true
          }]
        }]
      }]),
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-query-contract", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-query-contract-failure",
      segmentId: boundary.segmentId,
      actionId: "inspect-query-contract",
      actionName: "inspect_schema",
      input: {}
    });

    let failure: unknown;
    try {
      await boundary.actionRouter.execute({
        runId: "run-query-contract-failure",
        segmentId: boundary.segmentId,
        actionId: "sql-query-contract",
        actionName: "run_sql_readonly",
        input: {
          schema_id: "schema-1",
          requirement_ids: ["R1"],
          assertion_ids: ["R1.A1"],
          sql: "SELECT COUNT(*) AS orders FROM orders WHERE order_date >= '2023-07-01'"
        }
      });
    } catch (error) {
      failure = error;
    }

    expect(sqlExecuted).toBe(false);
    expect(failure).toBeInstanceOf(ToolExecutionError);
    expect((failure as ToolExecutionError).observation).toMatchObject({
      error: {
        code: "QUERY_CONTRACT_VALIDATION_FAILED",
        executionStatus: "not_started",
        message: expect.stringContaining("Required half-open end boundary 2024-01-01 is missing"),
        details: {
          queryAttemptId: "Q1",
          findings: [expect.objectContaining({
            code: "SQL_SEMANTIC_TIME_END_MISSING:order_date",
            severity: "error"
          })]
        }
      },
      recovery: {
        strategy: "refresh_and_replan",
        instruction: expect.stringContaining("Required half-open end boundary 2024-01-01 is missing"),
        avoid: [expect.stringContaining("same invalid SQL")]
      }
    });
    const state = boundary.protocolRuntime.getState("run-query-contract-failure");
    expect(state.phase).toBe("query_planning");
    expect(state.actions.map((action) => action.actionName)).toEqual([
      "inspect_schema",
      "semantic.context.resolve",
      "analysis.contract.ground",
      "data.query.plan",
      "data.query.validate"
    ]);
  });

  it.each([
    "report.md",
    "report.markdown",
    "report.txt",
    "report.html",
    "report.md/ "
  ])("requires evidenced claims to be committed before writing synthesis output %s", async (reportPath) => {
    let reportWrites = 0;
    const boundary = await createRunProtocolBoundary({
      runId: "run-report-commit-order",
      userInput: "计算订单数并输出 Markdown 报告",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      explicitProtocol: { protocolId: "data-analysis", protocolVersion: "1" },
      initialContextPackageRef: { packageId: "context-report-order", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-orders",
            audit_log_id: "audit-orders",
            columns: ["order_count"],
            rows: [[10]],
            row_count: 1
          }
        }) },
        write_file: {
          execute: async () => {
            reportWrites += 1;
            return { ok: true };
          }
        }
      },
      requirementExtractor: async () => createUserAnalysisRequirements([{
        kind: "metric",
        description: "计算订单数",
        acceptanceCriteria: ["报告订单总数"]
      }]),
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-report-order", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-report-commit-order",
      segmentId: boundary.segmentId,
      actionId: "inspect-report-order",
      actionName: "inspect_schema",
      input: {}
    });
    await boundary.actionRouter.execute({
      runId: "run-report-commit-order",
      segmentId: boundary.segmentId,
      actionId: "sql-report-order",
      actionName: "run_sql_readonly",
      input: {
        schema_id: "schema-1",
        requirement_ids: ["R1"],
        sql: "select 10 as order_count"
      }
    });

    await expect(boundary.actionRouter.execute({
      runId: "run-report-commit-order",
      segmentId: boundary.segmentId,
      actionId: "write-report-too-early",
      actionName: "write_file",
      input: { path: reportPath, content: "# report" }
    })).rejects.toMatchObject({
      observation: {
        error: {
          code: "ANALYSIS_REQUIREMENTS_COMMIT_REQUIRED",
          details: { requirementIds: ["R1"] }
        }
      }
    });
    expect(reportWrites).toBe(0);

    await boundary.actionRouter.execute({
      runId: "run-report-commit-order",
      segmentId: boundary.segmentId,
      actionId: "commit-report-order",
      actionName: "analysis.requirements.commit",
      input: { claims: [{ requirement_id: "R1", claim: "订单数为 10" }] }
    });
    await boundary.actionRouter.execute({
      runId: "run-report-commit-order",
      segmentId: boundary.segmentId,
      actionId: "write-report-after-commit",
      actionName: "write_file",
      input: { path: reportPath, content: "# report" }
    });
    expect(reportWrites).toBe(1);
  });

  it("supports more than one hundred governed actions in a complex data run", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-complex-budget",
      userInput: "执行复杂多轮分析",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-budget", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({
          result: {
            artifact_id: "artifact-budget",
            audit_log_id: "audit-budget",
            columns: ["value"],
            rows: [[1]],
            row_count: 1
          }
        }) }
      },
      semanticProvider: liveSemanticProvider(),
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-budget", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-complex-budget",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: {}
    });
    for (let index = 0; index < 21; index += 1) {
      await boundary.actionRouter.execute({
        runId: "run-complex-budget",
        segmentId: boundary.segmentId,
        actionId: `sql-${index + 1}`,
        actionName: "run_sql_readonly",
        input: { schema_id: "schema-1", sql: `SELECT ${index + 1}` }
      });
    }

    expect(boundary.protocolRuntime.getState("run-complex-budget").actions.length).toBeGreaterThan(100);
  });

  it("recovers after a SQL execution failure and completes a later query attempt", async () => {
    let sqlCalls = 0;
    const boundary = await createRunProtocolBoundary({
      runId: "run-sql-recovery",
      userInput: "分析订单并在查询失败后重试",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-recovery", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        preview_table: { execute: async () => ({ rows: [[1]] }) },
        run_sql_readonly: {
          execute: async () => {
            sqlCalls += 1;
            if (sqlCalls === 2) {
              throw new Error("no such column: missing_column");
            }
            return {
              result: {
                artifact_id: `artifact-${sqlCalls}`,
                audit_log_id: `audit-${sqlCalls}`,
                columns: ["value"],
                rows: [[sqlCalls]],
                row_count: 1
              }
            };
          }
        }
      },
      semanticProvider: {
        resolve: async (request) => ({
          value: {},
          capabilities: ["graph-explore"],
          trust: "verified",
          warnings: [],
          provider: "datalink",
          mode: "live",
          datasourceRevision: request.datasourceRevision
        })
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      projectContext: () => ({ packageId: "context-recovery", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: { datasource_id: "orders-db" }
    });
    await boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "preview-1",
      actionName: "preview_table",
      input: { schema_id: "schema-1", table: "orders" }
    });
    await boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "sql-1",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "select 1" }
    });
    await expect(boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "sql-2",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "select missing_column" }
    })).rejects.toThrow("no such column: missing_column");

    expect(boundary.protocolRuntime.getState("run-sql-recovery")).toMatchObject({
      phase: "execution",
      domain: { queryExecuted: false, validationPassed: false }
    });
    await boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "inspect-2",
      actionName: "inspect_schema",
      input: { datasource_id: "orders-db" }
    });
    await boundary.actionRouter.execute({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      actionId: "sql-3",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "select 3" }
    });

    const state = boundary.protocolRuntime.getState("run-sql-recovery");
    expect(state.phase).toBe("synthesis");
    expect(state.actions.filter((action) => action.status === "failed")).toHaveLength(1);
    const terminal = boundary.protocolRuntime.proposeCompletion({
      runId: "run-sql-recovery",
      segmentId: boundary.segmentId,
      expectedRevision: state.revision
    });
    expect(terminal.terminalDecision?.status).toBe("completed");
  });

  it("does not validate a SQL result that lacks tabular audit evidence", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-invalid-result",
      userInput: "分析订单",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-invalid-result", revision: 0 },
      tools: {
        inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) },
        run_sql_readonly: { execute: async () => ({ result: { artifact_id: "artifact-without-rows" } }) }
      },
      semanticProvider: {
        resolve: async (request) => ({
          value: {},
          capabilities: ["graph-explore"],
          trust: "verified",
          warnings: [],
          provider: "datalink",
          mode: "live",
          datasourceRevision: request.datasourceRevision
        })
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      projectContext: () => ({ packageId: "context-invalid-result", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-invalid-result",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: {}
    });
    await boundary.actionRouter.execute({
      runId: "run-invalid-result",
      segmentId: boundary.segmentId,
      actionId: "sql-1",
      actionName: "run_sql_readonly",
      input: { schema_id: "schema-1", sql: "select 1" }
    });

    const state = boundary.protocolRuntime.getState("run-invalid-result");
    expect(state).toMatchObject({ phase: "validation", domain: { validationPassed: false } });
    const completion = boundary.protocolRuntime.proposeCompletion({
      runId: "run-invalid-result",
      segmentId: boundary.segmentId,
      expectedRevision: state.revision
    });
    expect(completion.terminalDecision).toBeUndefined();
  });

  it("journals routing events before protocol segment start", async () => {
    const eventTypes: string[] = [];

    await createRunProtocolBoundary({
      runId: "run-route-events",
      userInput: "你好",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-route", revision: 0 },
      tools: {},
      projectContext: () => ({ packageId: "context-route", revision: 0 }),
      runtimeOptions: { onEvent: (event) => eventTypes.push(event.type) }
    });

    expect(eventTypes.slice(0, 4)).toEqual([
      "protocol.route.requested",
      "protocol.route.resolved",
      "protocol.run.started",
      "protocol.phase.entered"
    ]);
  });

  it("emits a routing failure before model assembly is allowed to continue", async () => {
    const eventTypes: string[] = [];

    await expect(createRunProtocolBoundary({
      runId: "run-route-failed",
      userInput: "分析数据",
      authorizedProtocolIds: ["general-task"],
      explicitProtocol: { protocolId: "data-analysis", protocolVersion: "1" },
      initialContextPackageRef: { packageId: "context-route", revision: 0 },
      tools: {},
      projectContext: () => ({ packageId: "context-route", revision: 0 }),
      runtimeOptions: { onEvent: (event) => eventTypes.push(event.type) }
    })).rejects.toThrow("PROTOCOL_NOT_AUTHORIZED:data-analysis@1");

    expect(eventTypes).toEqual(["protocol.route.failed"]);
  });

  it("switches the active runtime after an Agent handoff proposal is accepted", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-agent-handoff",
      userInput: "先解释，随后分析",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      explicitProtocol: { protocolId: "general-task", protocolVersion: "1" },
      initialContextPackageRef: { packageId: "context-handoff", revision: 0 },
      tools: { inspect_schema: { execute: async () => ({ schema_id: "schema-1" }) } },
      semanticProvider: {
        resolve: async (request) => ({
          value: {},
          capabilities: ["graph-explore"],
          trust: "verified",
          warnings: [],
          provider: "datalink",
          mode: "live",
          datasourceRevision: request.datasourceRevision
        })
      },
      semanticRequest: {
        userId: "user-1",
        workspaceId: "workspace-1",
        datasourceId: "orders-db",
        datasourceRevision: "schema-v1"
      },
      projectContext: () => ({ packageId: "context-handoff", revision: 1 })
    });

    await boundary.actionRouter.execute({
      runId: "run-agent-handoff",
      segmentId: boundary.segmentId,
      actionId: "handoff-1",
      actionName: "protocol.handoff.propose",
      input: {
        targetProtocolId: "data-analysis",
        targetProtocolVersion: "1",
        reasonCodes: ["ANALYTIC_INTENT"],
        unresolvedGoals: []
      }
    });

    expect(boundary.segmentId).toBe("run-agent-handoff:segment:2");
    expect(boundary.protocolRuntime.getState("run-agent-handoff")).toMatchObject({
      protocolId: "data-analysis",
      phase: "scope",
      status: "active"
    });
    await boundary.actionRouter.execute({
      runId: "run-agent-handoff",
      segmentId: boundary.segmentId,
      actionId: "inspect-after-handoff",
      actionName: "inspect_schema",
      input: {}
    });
    expect(boundary.protocolRuntime.getState("run-agent-handoff").phase).toBe("query_planning");
  });

  it("inherits the session intent deterministically for a weak follow-up without calling the classifier", async () => {
    let classifierCalls = 0;
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-inherit",
      userInput: "再次尝试",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent", revision: 0 },
      tools: {},
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" },
      classifier: async () => {
        classifierCalls += 1;
        return { protocolId: "general-task", protocolVersion: "1", confidence: 0.99, reasonCodes: ["WRONG"] };
      },
      projectContext: () => ({ packageId: "context-intent", revision: 0 })
    });

    expect(boundary.route.definition.id).toBe("data-analysis");
    expect(boundary.route.source).toBe("deterministic");
    expect(boundary.route.reasonCodes).toEqual(["SESSION_INTENT_INHERITED"]);
    expect(classifierCalls).toBe(0);
  });

  it("outranks the keyword accelerator with the recorded session intent", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-vs-regex",
      userInput: "重试统计",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent-2", revision: 0 },
      tools: {},
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "统计订单量" },
      projectContext: () => ({ packageId: "context-intent-2", revision: 0 })
    });

    expect(boundary.route.source).toBe("deterministic");
    expect(boundary.route.reasonCodes).toEqual(["SESSION_INTENT_INHERITED"]);
  });

  it("ignores an unauthorized session intent and falls back to the normal route", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-unauthorized",
      userInput: "再次尝试",
      authorizedProtocolIds: ["general-task"],
      initialContextPackageRef: { packageId: "context-intent-3", revision: 0 },
      tools: {},
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "分析" },
      projectContext: () => ({ packageId: "context-intent-3", revision: 0 })
    });

    expect(boundary.route.definition.id).toBe("general-task");
    expect(boundary.route.source).toBe("default");
  });

  it("passes the session intent to the classifier for ambiguous non-continuation input", async () => {
    const classificationInputs: unknown[] = [];
    await createRunProtocolBoundary({
      runId: "run-intent-classifier-context",
      userInput: "把结果按地区分组再排一下",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent-4", revision: 0 },
      tools: {},
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" },
      classifier: async ({ value }) => {
        classificationInputs.push(value);
        return { protocolId: "data-analysis", protocolVersion: "1", confidence: 0.9, reasonCodes: ["FOLLOW_UP"] };
      },
      projectContext: () => ({ packageId: "context-intent-4", revision: 0 })
    });

    expect(classificationInputs).toEqual([{
      userText: "把结果按地区分组再排一下",
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" }
    }]);
  });

  it("extracts requirements from the intent text when a weak follow-up inherits data-analysis", async () => {
    const extractorInputs: string[] = [];
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-extraction",
      userInput: "再次尝试",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent-extract", revision: 0 },
      tools: {},
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" },
      requirementExtractor: async ({ userText }) => {
        extractorInputs.push(userText);
        return createUserAnalysisRequirements([
          { kind: "metric", description: "分析当前数据", acceptanceCriteria: ["给出结论"] }
        ]);
      },
      projectContext: () => ({ packageId: "context-intent-extract", revision: 0 })
    });

    expect(extractorInputs).toEqual(["帮我分析当前数据\n(后续指示: 再次尝试)"]);
    expect(boundary.protocolRuntime.getState("run-intent-extraction").domain).toMatchObject({
      requirements: expect.arrayContaining([expect.objectContaining({ id: "R1", description: "分析当前数据" })])
    });
  });

  it("does not extract requirements when the route resolves to general-task", async () => {
    let extractorCalls = 0;
    const boundary = await createRunProtocolBoundary({
      runId: "run-general-no-extraction",
      userInput: "你好",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-general", revision: 0 },
      tools: {},
      requirementExtractor: async () => {
        extractorCalls += 1;
        return [];
      },
      projectContext: () => ({ packageId: "context-general", revision: 0 })
    });

    expect(boundary.route.definition.id).toBe("general-task");
    expect(extractorCalls).toBe(0);
  });

  it("sends the intent text, not the weak follow-up, as the semantic resolution query", async () => {
    const semanticQueries: string[] = [];
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-semantic-query",
      userInput: "再次尝试",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent-semantic", revision: 0 },
      tools: { inspect_schema: { execute: async () => ({ schema_id: "schema-1", tables: [] }) } },
      sessionIntent: { protocolId: "data-analysis", protocolVersion: "1", intentText: "帮我分析当前数据" },
      semanticProvider: {
        resolve: async (request) => {
          semanticQueries.push(request.query);
          return {
            value: {},
            capabilities: ["graph-explore"],
            trust: "verified" as const,
            warnings: [],
            provider: "datalink" as const,
            mode: "live" as const,
            datasourceRevision: request.datasourceRevision
          };
        }
      },
      semanticRequest: semanticRequest(),
      projectContext: () => ({ packageId: "context-intent-semantic", revision: 0 })
    });

    await boundary.actionRouter.execute({
      runId: "run-intent-semantic-query",
      segmentId: boundary.segmentId,
      actionId: "inspect-1",
      actionName: "inspect_schema",
      input: {}
    });

    expect(semanticQueries).toEqual(["帮我分析当前数据\n(后续指示: 再次尝试)"]);
  });

  it("extracts requirements at handoff when a general-task run moves to data-analysis", async () => {
    const extractorInputs: string[] = [];
    const boundary = await createRunProtocolBoundary({
      runId: "run-handoff-extraction",
      userInput: "先聊聊,可能要查数",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      explicitProtocol: { protocolId: "general-task", protocolVersion: "1" },
      initialContextPackageRef: { packageId: "context-handoff-extract", revision: 0 },
      tools: {},
      requirementExtractor: async ({ userText }) => {
        extractorInputs.push(userText);
        return createUserAnalysisRequirements([
          { kind: "metric", description: "查数", acceptanceCriteria: ["有证据"] }
        ]);
      },
      projectContext: () => ({ packageId: "context-handoff-extract", revision: 0 })
    });
    expect(extractorInputs).toEqual([]);

    await boundary.actionRouter.execute({
      runId: "run-handoff-extraction",
      segmentId: boundary.segmentId,
      actionId: "handoff-1",
      actionName: "protocol.handoff.propose",
      input: {
        targetProtocolId: "data-analysis",
        targetProtocolVersion: "1",
        reasonCodes: ["ANALYTIC_INTENT"],
        unresolvedGoals: []
      }
    });

    expect(extractorInputs).toEqual(["先聊聊,可能要查数"]);
    expect(boundary.protocolRuntime.getState("run-handoff-extraction").domain).toMatchObject({
      requirements: expect.arrayContaining([expect.objectContaining({ description: "查数" })])
    });
  });

  it("keeps weak follow-ups on the default route when no session intent exists", async () => {
    const boundary = await createRunProtocolBoundary({
      runId: "run-intent-none",
      userInput: "再次尝试",
      authorizedProtocolIds: ["general-task", "data-analysis"],
      initialContextPackageRef: { packageId: "context-intent-5", revision: 0 },
      tools: {},
      classifier: async () => ({
        protocolId: "data-analysis",
        protocolVersion: "1",
        confidence: 0.4,
        reasonCodes: ["WEAK"]
      }),
      projectContext: () => ({ packageId: "context-intent-5", revision: 0 })
    });

    expect(boundary.route.definition.id).toBe("general-task");
    expect(boundary.route.source).toBe("default");
    expect(boundary.route.warnings).toEqual(["PROTOCOL_CLASSIFICATION_LOW_CONFIDENCE"]);
  });
});

const liveSemanticProvider = (): { resolve(request: SemanticRequest): Promise<SemanticResolution> } => ({
  resolve: async (request: SemanticRequest) => ({
    value: {},
    capabilities: ["graph-explore"],
    trust: "verified" as const,
    warnings: [],
    provider: "datalink",
    mode: "live",
    datasourceRevision: request.datasourceRevision
  })
});

const semanticRequest = () => ({
  userId: "user-1",
  workspaceId: "workspace-1",
  datasourceId: "orders-db",
  datasourceRevision: "schema-v1"
});
