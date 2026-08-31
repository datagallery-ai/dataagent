import { describe, expect, it } from "vitest";

import { createUserAnalysisRequirements } from "../analysis-requirements.js";
import { createDataAnalysisProtocol, reduceDataAnalysisAction } from "./data-analysis.js";
import { createGeneralTaskProtocol, reduceGeneralTaskAction } from "./general-task.js";

const contextPackageRef = { packageId: "context-1", revision: 0 };

describe("formal protocols", () => {
  it("keeps data tools out of the general-task protocol", () => {
    const protocol = createGeneralTaskProtocol([
      "retrieve_knowledge",
      "read_file",
      "inspect_schema",
      "run_sql_readonly"
    ]);

    expect(protocol.initialPhase).toBe("understand");
    expect(protocol.phases.understand?.allowedActions).toContain("retrieve_knowledge");
    expect(protocol.phases.understand?.allowedActions).not.toContain("inspect_schema");
    expect(protocol.phases.gather?.allowedActions).not.toContain("run_sql_readonly");
    expect(protocol.phases.answer?.allowedActions).toEqual([]);
  });

  it("completes a general task only after its answer is committed", () => {
    const protocol = createGeneralTaskProtocol([]);
    const initial = protocol.createInitialState({ contextPackageRef, runId: "run-1" });
    expect(protocol.completionPolicy({ contextPackageRef, state: initial }).status).toBe("continue");

    const committed = reduceGeneralTaskAction(initial, "general.answer.commit", { messageId: "message-1" });
    expect(protocol.completionPolicy({ contextPackageRef, state: committed }).status).toBe("completed");
  });

  it("rejects a general answer commit without a message id", () => {
    expect(() => reduceGeneralTaskAction({}, "general.answer.commit", {}))
      .toThrow("GENERAL_ANSWER_MESSAGE_MISSING");
  });

  it("does not allow SQL before schema grounding", () => {
    const protocol = createDataAnalysisProtocol(["inspect_schema", "run_sql_readonly"]);

    expect(protocol.initialPhase).toBe("scope");
    expect(protocol.phases.scope?.allowedActions).toContain("inspect_schema");
    expect(protocol.phases.semantic_grounding?.allowedActions).not.toContain("run_sql_readonly");
    expect(protocol.phases.query_planning?.allowedActions).toContain("data.query.validate");
    expect(protocol.phases.query_planning?.allowedActions).not.toContain("run_sql_readonly");
    expect(protocol.phases.execution?.allowedActions).toContain("run_sql_readonly");
  });

  it("requires validated evidence before completing data analysis", () => {
    const protocol = createDataAnalysisProtocol(["inspect_schema", "run_sql_readonly"]);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-1" });
    state = reduceDataAnalysisAction(state, "inspect_schema", {});
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", {
      mode: "live",
      trust: "verified",
      warnings: []
    });
    state = reduceDataAnalysisAction(state, "data.query.plan", { sql: "select 1" });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", { artifact_id: "artifact-1" });
    expect(protocol.completionPolicy({ contextPackageRef, state }).status).toBe("continue");

    state = reduceDataAnalysisAction(state, "analysis.result.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", { evidence_refs: ["artifact-1"] });
    expect(protocol.completionPolicy({ contextPackageRef, state }).status).toBe("completed");
  });

  it("discloses local semantic fallback as degraded completion", () => {
    const protocol = createDataAnalysisProtocol([]);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-1" });
    state = reduceDataAnalysisAction(state, "inspect_schema", {});
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", {
      mode: "fallback",
      trust: "verified",
      warnings: ["LOCAL_SEMANTIC_LIMITED_TO_PHYSICAL_SCHEMA"]
    });
    state = reduceDataAnalysisAction(state, "data.query.plan", {});
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", { artifact_id: "artifact-1" });
    state = reduceDataAnalysisAction(state, "analysis.result.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", { evidence_refs: ["artifact-1"] });

    expect(protocol.completionPolicy({ contextPackageRef, state })).toMatchObject({
      status: "degraded",
      reasons: ["LOCAL_SEMANTIC_LIMITED_TO_PHYSICAL_SCHEMA"]
    });
  });

  it("preserves a grounded contract when the same schema is inspected again", () => {
    const requirements = createUserAnalysisRequirements([{
      kind: "metric",
      description: "统计订单数",
      acceptanceCriteria: ["返回订单总数"],
      assertions: [{
        kind: "metric",
        description: "订单数",
        sourceTables: ["orders"],
        claimValues: [{ name: "order_count", field: "order_count", required: true }]
      }]
    }]);
    const protocol = createDataAnalysisProtocol([], requirements);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-reinspect" });
    state = reduceDataAnalysisAction(state, "inspect_schema", { schema_id: "schema-orders" });
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", { mode: "live", trust: "verified" });
    state = reduceDataAnalysisAction(state, "analysis.contract.ground", {
      schema_id: "schema-orders",
      datasourceRevision: "revision-1",
      requirements
    });

    const refreshedSnapshot = reduceDataAnalysisAction(state, "inspect_schema", { schema_id: "schema-orders-refresh" });
    const sameRevision = reduceDataAnalysisAction(refreshedSnapshot, "semantic.context.resolve", {
      mode: "live",
      trust: "verified",
      datasourceRevision: "revision-1"
    });
    const changedRevision = reduceDataAnalysisAction(refreshedSnapshot, "semantic.context.resolve", {
      mode: "live",
      trust: "verified",
      datasourceRevision: "revision-2"
    });

    expect(refreshedSnapshot.contractGrounded).toBe(true);
    expect(sameRevision.contractGrounded).toBe(true);
    expect(changedRevision.contractGrounded).toBe(false);
  });

  it("keeps a later query attempt pending until it has its own validated evidence", () => {
    const protocol = createDataAnalysisProtocol([]);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-1" });
    state = reduceDataAnalysisAction(state, "inspect_schema", {});
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", { mode: "live", warnings: [] });
    state = reduceDataAnalysisAction(state, "data.query.plan", { sql: "select 1" });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", { artifact_id: "artifact-1" });
    state = reduceDataAnalysisAction(state, "analysis.result.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", { evidence_refs: ["artifact-1"] });
    state = reduceDataAnalysisAction(state, "data.query.plan", { sql: "delete from orders" });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: false });

    expect(state).toMatchObject({
      currentQueryValidated: false,
      queryExecuted: false,
      validationPassed: false
    });
    expect(protocol.completionPolicy({ contextPackageRef, state }).status).toBe("continue");
  });

  it("keeps SQL semantic failures on the original attempt and validates a repaired attempt", () => {
    const requirements = createUserAnalysisRequirements([{
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
    }]);
    const protocol = createDataAnalysisProtocol([], requirements);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-semantic-repair" });
    state = reduceDataAnalysisAction(state, "inspect_schema", { dialect: "sqlite" });
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", { mode: "fallback", warnings: [] });
    state = reduceDataAnalysisAction(state, "data.query.plan", {
      requirement_ids: ["R1"],
      assertion_ids: ["R1.A1"],
      sql: "SELECT COUNT(*) AS orders FROM orders WHERE order_date >= '2023-07-01' AND order_date < '2023-12-31'"
    });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true, reasons: [] });

    expect(state.currentQueryValidated).toBe(false);
    expect(state.queryAttempts[0]).toMatchObject({
      id: "Q1",
      valid: false,
      validationFindings: [{ code: "SQL_SEMANTIC_TIME_END_MISSING:order_date" }]
    });

    state = reduceDataAnalysisAction(state, "data.query.plan", {
      requirement_ids: ["R1"],
      assertion_ids: ["R1.A1"],
      sql: "SELECT COUNT(*) AS orders FROM orders WHERE order_date >= '2023-07-01' AND order_date < '2024-01-01'"
    });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true, reasons: [] });

    expect(state.currentQueryValidated).toBe(true);
    expect(state.queryAttempts).toMatchObject([
      { id: "Q1", valid: false },
      { id: "Q2", valid: true, validationFindings: [] }
    ]);
  });

  it("requires every user requirement to bind validated evidence and a reported claim", () => {
    const requirements = createUserAnalysisRequirements([
      { kind: "data_quality", description: "核对利润公式", acceptanceCriteria: ["报告错误数"] },
      { kind: "metric", description: "计算新增利润", acceptanceCriteria: ["精确到分"] }
    ]);
    const protocol = createDataAnalysisProtocol([], requirements);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-requirements" });
    state = reduceDataAnalysisAction(state, "inspect_schema", {});
    state = reduceDataAnalysisAction(state, "semantic.context.resolve", { mode: "live", warnings: [] });
    state = reduceDataAnalysisAction(state, "data.query.plan", {
      requirement_ids: ["R1"],
      expected_columns: ["formula_mismatches"],
      sql: "select 0 as formula_mismatches"
    });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", {
      result: {
        artifact_id: "artifact-1",
        audit_log_id: "audit-1",
        columns: ["formula_mismatches"],
        rows: [[0]],
        row_count: 1
      }
    });
    state = reduceDataAnalysisAction(state, "analysis.result.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", {
      artifact_id: "artifact-1",
      audit_log_id: "audit-1",
      evidence_refs: ["artifact-1"],
      result_fields: ["formula_mismatches"]
    });

    expect(state.requirements.find((requirement) => requirement.id === "R1")?.status).toBe("evidenced");
    expect(protocol.completionPolicy({ contextPackageRef, state })).toMatchObject({
      status: "continue",
      reasons: expect.arrayContaining(["ANALYSIS_REQUIREMENT_NOT_REPORTED:R1", "ANALYSIS_REQUIREMENT_PENDING:R2"])
    });

    const evidenceId = state.evidenceBindings.find((binding) => binding.requirementId === "R1")?.id;
    expect(evidenceId).toBeDefined();
    const stateWithExtraEvidence = reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "利润公式错误数为 0",
        evidence_refs: ["artifact-1", "unrelated-artifact"]
      }]
    });
    expect(stateWithExtraEvidence.requirements.find((requirement) => requirement.id === "R1")?.status)
      .toBe("reported");
    const stateWithResolvedEvidence = reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{ requirement_id: "R1", claim: "利润公式错误数为 0" }]
    });
    expect(stateWithResolvedEvidence.reportedClaims[0]?.evidenceBindingIds).toEqual([evidenceId]);
    const stateWithSourceRequirement = reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R2",
        claim: "新增利润基于已验证的利润公式",
        evidence_requirement_ids: ["R1"]
      }]
    });
    expect(stateWithSourceRequirement.reportedClaims[0]?.evidenceBindingIds).toEqual([evidenceId]);
    state = reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{ requirement_id: "R1", claim: "利润公式错误数为 0", evidence_binding_ids: [evidenceId] }]
    });
    expect(state.requirements.find((requirement) => requirement.id === "R1")?.status).toBe("reported");
    expect(protocol.completionPolicy({ contextPackageRef, state }).status).toBe("continue");
  });

  it("links existing Mastra tasks to requirements without treating task completion as evidence", () => {
    const requirements = createUserAnalysisRequirements([
      { kind: "comparison", description: "比较冻结组合", acceptanceCriteria: [] }
    ]);
    const protocol = createDataAnalysisProtocol([], requirements);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-task-link" });

    state = reduceDataAnalysisAction(state, "task_write", {
      tasks: [{ id: "task-holdout", content: "[R1] 比较冻结组合", activeForm: "比较中", status: "pending" }]
    });
    state = reduceDataAnalysisAction(state, "task_complete", {
      tasks: [{ id: "task-holdout", content: "[R1] 比较冻结组合", activeForm: "比较中", status: "completed" }]
    });

    expect(state.taskRequirementLinks).toEqual([{ taskId: "task-holdout", requirementIds: ["R1"] }]);
    expect(state.requirements.find((requirement) => requirement.id === "R1")).toMatchObject({
      status: "pending",
      taskIds: ["task-holdout"]
    });
  });

  it("accepts claim values only when they match deterministic result verification", () => {
    const requirements = createUserAnalysisRequirements([{
      kind: "metric",
      description: "计算订单转化率",
      acceptanceCriteria: ["结果可由分子分母复算"],
      assertions: [{
        kind: "metric",
        description: "订单转化率",
        resultChecks: [{
          kind: "ratio",
          required: true,
          value: { field: "conversion_rate" },
          numerator: { field: "converted" },
          denominator: { field: "total" }
        }],
        claimValues: [{
          name: "conversion_rate",
          field: "conversion_rate",
          unit: "%",
          required: true,
          tolerance: 0.000001
        }]
      }]
    }]);
    const protocol = createDataAnalysisProtocol([], requirements);
    let state = protocol.createInitialState({ contextPackageRef, runId: "run-claim-value" });
    state = reduceDataAnalysisAction(state, "data.query.plan", {
      sql: "select 40 as converted, 100 as total, 0.4 as conversion_rate",
      requirement_ids: ["R1"],
      assertion_ids: ["R1.A1"]
    });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", {
      result: {
        artifact_id: "artifact-ratio",
        audit_log_id: "audit-ratio",
        columns: ["converted", "total", "conversion_rate"]
      }
    });
    state = reduceDataAnalysisAction(state, "analysis.result.validate", {
      valid: true,
      validation_findings: [],
      verified_values: [{
        name: "conversion_rate",
        value: 0.4,
        unit: "%",
        tolerance: 0.000001,
        assertionId: "R1.A1"
      }]
    });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", {
      artifact_id: "artifact-ratio",
      audit_log_id: "audit-ratio",
      evidence_refs: ["artifact-ratio"]
    });

    expect(() => reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "订单转化率为 50%",
        values: [{ name: "conversion_rate", value: 0.5, unit: "%" }]
      }]
    })).toThrow(
      'ANALYSIS_CLAIM_VALUE_MISMATCH:R1:conversion_rate: expected value=0.4, unit="%", tolerance=0.000001; '
      + 'received value=0.5, unit="%".'
    );
    expect(() => reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{ requirement_id: "R1", claim: "订单转化率为 40%" }]
    })).toThrow(
      'ANALYSIS_CLAIM_VALUE_REQUIRED:R1:conversion_rate: submit value=0.4, unit="%", tolerance=0.000001.'
    );
    expect(() => reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "提交未验证指标",
        values: [{ name: "unknown_rate", value: 0.4 }]
      }]
    })).toThrow(
      "ANALYSIS_CLAIM_VALUE_UNKNOWN:R1:unknown_rate: use an exact verified name; allowed names: conversion_rate."
    );
    expect(() => reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "订单转化率为 40",
        values: [{ name: "conversion_rate", value: 0.4 }]
      }]
    })).toThrow(
      'ANALYSIS_CLAIM_VALUE_MISMATCH:R1:conversion_rate: expected value=0.4, unit="%", tolerance=0.000001; '
      + "received value=0.4, no unit."
    );

    const committed = reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "订单转化率为 40%",
        values: [{ name: "conversion_rate", value: 0.4, unit: "%" }]
      }]
    });
    expect(committed.reportedClaims[0]?.values).toEqual([
      { name: "conversion_rate", value: 0.4, unit: "%" }
    ]);
  });

  it("does not report a requirement until every required assertion value has verified evidence", () => {
    const multiAssertionRequirements = createUserAnalysisRequirements([{
      kind: "metric",
      description: "订单数和 GMV",
      acceptanceCriteria: ["返回两个指标"],
      assertions: [
        {
          kind: "metric",
          description: "订单数",
          claimValues: [{ name: "order_count", field: "order_count", required: true }]
        },
        {
          kind: "metric",
          description: "GMV",
          claimValues: [{ name: "gmv_total", field: "gmv_total", required: true }]
        }
      ]
    }]);
    let state = createDataAnalysisProtocol([], multiAssertionRequirements)
      .createInitialState({ contextPackageRef, runId: "run-partial-assertion" });
    state = reduceDataAnalysisAction(state, "data.query.plan", {
      sql: "select count(*) as order_count from orders",
      requirement_ids: ["R1"],
      assertion_ids: ["R1.A1"]
    });
    state = reduceDataAnalysisAction(state, "data.query.validate", { valid: true });
    state = reduceDataAnalysisAction(state, "run_sql_readonly", {
      result: {
        artifact_id: "artifact-order-count",
        audit_log_id: "audit-order-count",
        columns: ["order_count"]
      }
    });
    state = reduceDataAnalysisAction(state, "analysis.result.validate", {
      valid: true,
      validation_findings: [],
      verified_values: [{ name: "order_count", value: 3, tolerance: 0, assertionId: "R1.A1" }]
    });
    state = reduceDataAnalysisAction(state, "analysis.evidence.bind", {
      artifact_id: "artifact-order-count",
      audit_log_id: "audit-order-count",
      evidence_refs: ["artifact-order-count"]
    });

    expect(() => reduceDataAnalysisAction(state, "analysis.requirements.commit", {
      claims: [{
        requirement_id: "R1",
        claim: "订单数为 3",
        values: [{ name: "order_count", value: 3 }]
      }]
    })).toThrow(
      "ANALYSIS_CLAIM_VALUE_NOT_VERIFIED:R1:gmv_total: execute and bind evidence for assertion R1.A2 before committing"
    );
  });

  it("derives requirement ids from server-owned assertion ids", () => {
    const requirements = createUserAnalysisRequirements([{
      kind: "metric",
      description: "计算订单数",
      acceptanceCriteria: ["返回订单总数"],
      assertions: [{
        kind: "metric",
        description: "订单总数",
        sourceTables: ["orders"]
      }]
    }]);
    const protocol = createDataAnalysisProtocol([], requirements);
    const initial = protocol.createInitialState({ contextPackageRef, runId: "run-derived-requirement-ids" });

    const planned = reduceDataAnalysisAction(initial, "data.query.plan", {
      assertion_ids: ["R1.A1"],
      sql: "select count(*) from orders"
    });

    expect(planned.currentQueryAttemptId).toBe("Q1");
    expect(planned.queryAttempts[0]).toMatchObject({
      requirementIds: ["R1"],
      assertionIds: ["R1.A1"]
    });
  });

  it("rejects unknown requirement ids and evidence from another requirement", () => {
    const requirements = createUserAnalysisRequirements([
      { kind: "metric", description: "计算利润", acceptanceCriteria: [] }
    ]);
    const protocol = createDataAnalysisProtocol([], requirements);
    const initial = protocol.createInitialState({ contextPackageRef, runId: "run-invalid-binding" });

    expect(() => reduceDataAnalysisAction(initial, "data.query.plan", {
      requirement_ids: ["R404"],
      sql: "select 1"
    })).toThrow("ANALYSIS_REQUIREMENT_NOT_FOUND:R404");
    expect(() => reduceDataAnalysisAction(initial, "analysis.requirements.commit", {
      claims: [{ requirement_id: "R1", claim: "profit", evidence_binding_ids: ["E404"] }]
    })).toThrow("ANALYSIS_REQUIREMENT_EVIDENCE_INVALID:R1:E404");
  });
});
