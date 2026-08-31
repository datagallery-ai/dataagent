import { describe, expect, it } from "vitest";

import { createUserAnalysisRequirements } from "./analysis-requirements.js";
import {
  createFallbackAnalysisContractGrounding,
  createAnalysisContractGroundingPrompt,
  createAnalysisContractGroundingRetryInstruction,
  parseAnalysisContractGroundingText
} from "./model-analysis-contract-grounder.js";

const requirements = createUserAnalysisRequirements([{
  kind: "metric",
  description: "统计物流单量和日期范围",
  acceptanceCriteria: ["报告总单量、最早日期和最晚日期"]
}]);

const physicalSchema = {
  schema_id: "schema-1",
  tables: [{
    name: "dacomp-zh-006",
    columns: [
      { name: "日期", type: "DATE" },
      { name: "物流单号", type: "VARCHAR" }
    ]
  }]
};

describe("analysis contract grounding", () => {
  it("grounds assertions against the inspected physical schema with server-owned ids", () => {
    const result = parseAnalysisContractGroundingText(JSON.stringify({
      contracts: [{
        requirementId: "R1",
        assertions: [{
          kind: "metric",
          description: "物流单量与日期边界",
          sourceTables: ["dacomp-zh-006"],
          sqlConstraints: [
            { kind: "source", table: "dacomp-zh-006" },
            { kind: "column", column: "日期" },
            { kind: "aggregate", function: "COUNT", column: "物流单号", alias: "shipment_count" },
            { kind: "aggregate", function: "MIN", column: "日期", alias: "min_date" },
            { kind: "aggregate", function: "MAX", column: "日期", alias: "max_date" }
          ],
          resultChecks: [{
            kind: "not_null",
            required: true,
            fields: ["shipment_count", "min_date", "max_date"]
          }],
          claimValues: [
            { name: "shipment_count", field: "shipment_count", required: true },
            { name: "min_date", field: "min_date", required: true },
            { name: "max_date", field: "max_date", required: true }
          ]
        }]
      }]
    }), requirements, physicalSchema);

    expect(result.findings).toEqual([]);
    expect(result.requirements[0]).toMatchObject({
      id: "R1",
      description: "统计物流单量和日期范围",
      assertions: [{
        id: "R1.A1",
        requirementId: "R1",
        kind: "metric",
        sourceTables: ["dacomp-zh-006"]
      }]
    });
  });

  it("downgrades invented physical identifiers instead of making them authoritative", () => {
    const result = parseAnalysisContractGroundingText(JSON.stringify({
      contracts: [{
        requirementId: "R1",
        assertions: [{
          kind: "metric",
          description: "订单数",
          sourceTables: ["orders"],
          sqlConstraints: [{ kind: "column", column: "order_date" }],
          claimValues: [{ name: "order_count", field: "order_count", required: true }]
        }]
      }]
    }), requirements, physicalSchema);

    expect(result.requirements[0]?.assertions).toEqual([
      expect.objectContaining({ id: "R1.A1", kind: "manual", sourceTables: [], sqlConstraints: [] })
    ]);
    expect(result.findings).toEqual(expect.arrayContaining([
      expect.objectContaining({ requirementId: "R1", code: "CONTRACT_UNKNOWN_TABLE" }),
      expect.objectContaining({ requirementId: "R1", code: "CONTRACT_UNKNOWN_COLUMN" })
    ]));
  });

  it("rejects structured assertions that cannot produce runtime-verified claim values", () => {
    const output = JSON.stringify({
      contracts: [{
        requirementId: "R1",
        assertions: [{
          kind: "metric",
          description: "物流单量",
          sourceTables: ["dacomp-zh-006"],
          sqlConstraints: [{
            kind: "aggregate",
            function: "COUNT",
            column: "物流单号",
            alias: "shipment_count"
          }],
          resultChecks: [{ kind: "not_null", required: true, fields: ["shipment_count"] }]
        }]
      }]
    });

    expect(() => parseAnalysisContractGroundingText(output, requirements, physicalSchema))
      .toThrow("Structured assertions require at least one claimValues entry");
  });

  it("rejects a wildcard used as a standalone physical column constraint", () => {
    const output = JSON.stringify({
      contracts: [{
        requirementId: "R1",
        assertions: [{
          kind: "metric",
          description: "物流单量",
          sourceTables: ["dacomp-zh-006"],
          sqlConstraints: [
            { kind: "column", column: "*" },
            { kind: "aggregate", function: "COUNT", column: "*", alias: "shipment_count" }
          ],
          claimValues: [{ name: "shipment_count", field: "shipment_count", required: true }]
        }]
      }]
    });

    expect(() => parseAnalysisContractGroundingText(output, requirements, physicalSchema))
      .toThrow("Wildcard '*' is only valid as an aggregate operand");
  });

  it("makes schema and semantic grounding constraints explicit in the model prompt", () => {
    const prompt = createAnalysisContractGroundingPrompt({
      requirements,
      physicalSchema,
      semanticResolution: {
        provider: "datalink",
        mode: "live",
        datasourceRevision: "revision-7",
        value: { definitions: ["物流单号唯一标识一票物流"] },
        capabilities: ["graph-explore"],
        trust: "verified",
        warnings: []
      },
      datasourceRevision: "revision-7"
    });

    expect(prompt).toContain("dacomp-zh-006");
    expect(prompt).toContain("物流单号");
    expect(prompt).toContain("revision-7");
    expect(prompt).toContain("不得发明表名或字段名");
  });

  it("defines the exact nested JSON shapes required from the model", () => {
    const prompt = createAnalysisContractGroundingPrompt({
      requirements,
      physicalSchema,
      semanticResolution: {
        provider: "local",
        mode: "fallback",
        datasourceRevision: "revision-8",
        trust: "verified",
        value: { tables: ["dacomp-zh-006"] },
        capabilities: ["physical-schema"],
        warnings: []
      },
      datasourceRevision: "revision-8"
    });

    expect(prompt).toContain('{"kind":"source","table":"<schema table>"}');
    expect(prompt).toContain('{"kind":"aggregate","function":"COUNT","column":"*","alias":"row_count"}');
    expect(prompt).toContain('{"kind":"not_null","required":true,"fields":["row_count"]}');
    expect(prompt).toContain('{"name":"row_count","field":"row_count","required":true}');
    expect(prompt).toContain("每个 sqlConstraints 元素只能表达一种 kind");
    expect(prompt).toContain('"*" 只能作为 aggregate 的 column');
  });

  it("turns validation failures into precise retry instructions", () => {
    let parseError: unknown;
    try {
      parseAnalysisContractGroundingText(JSON.stringify({
        contracts: [{
          requirementId: "R1",
          assertions: [{
            kind: "metric",
            description: "物流单量",
            sourceTables: ["dacomp-zh-006"],
            sqlConstraints: [{ source: "dacomp-zh-006", aggregate: "COUNT(*)" }],
            resultChecks: ["结果必须为非负整数"]
          }]
        }]
      }), requirements, physicalSchema);
    } catch (error) {
      parseError = error;
    }

    const instruction = createAnalysisContractGroundingRetryInstruction(parseError);

    expect(instruction).toContain("contracts.0.assertions.0.sqlConstraints.0.kind");
    expect(instruction).toContain("Invalid discriminator value");
    expect(instruction).toContain("不要重复原来的无效结构");
  });

  it("preserves the final validation reason when grounding falls back to manual", () => {
    const parseError = new Error("contracts.0.assertions.0.claimValues.0.required is missing");

    const result = createFallbackAnalysisContractGrounding(requirements, parseError);

    expect(result.requirements[0]?.assertions).toEqual([
      expect.objectContaining({ kind: "manual" })
    ]);
    expect(result.findings).toEqual([
      expect.objectContaining({
        requirementId: "R1",
        code: "CONTRACT_INVALID_OUTPUT",
        message: expect.stringContaining("claimValues.0.required is missing")
      })
    ]);
  });
});
