import { describe, expect, it } from "vitest";

import {
  createAnalysisRequirementExtractionPrompt,
  createFallbackAnalysisRequirements,
  parseAnalysisRequirementExtractionText
} from "./model-analysis-requirement-extractor.js";

describe("analysis requirement extraction", () => {
  it("parses fenced JSON and assigns stable requirement ids", () => {
    const requirements = parseAnalysisRequirementExtractionText(`\`\`\`json
      {"requirements":[
        {"kind":"data_quality","description":"核对利润公式","acceptanceCriteria":["报告误差数量"]},
        {"kind":"metric","description":"计算新增利润","acceptanceCriteria":["精确到分"]}
      ]}
    \`\`\``);

    expect(requirements).toMatchObject([
      { id: "R1", kind: "data_quality", description: "核对利润公式", required: true, source: "user" },
      { id: "R2", kind: "metric", description: "计算新增利润", required: true, source: "user" }
    ]);
  });

  it("deduplicates equivalent descriptions without trusting model ids", () => {
    const requirements = parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [
        {
          id: "invented-1",
          kind: "comparison",
          description: "Compare holdout cohorts",
          acceptanceCriteria: ["show sample sizes"]
        },
        {
          id: "invented-2",
          kind: "comparison",
          description: "  compare holdout cohorts  ",
          acceptanceCriteria: ["duplicate"]
        }
      ]
    }));

    expect(requirements).toHaveLength(1);
    expect(requirements[0]?.id).toBe("R1");
  });

  it("normalizes a single acceptance criterion returned as a string", () => {
    const requirements = parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [{
        kind: "metric",
        description: "计算整体转化率",
        acceptanceCriteria: "报告分子、分母和比例"
      }]
    }));

    expect(requirements[0]?.acceptanceCriteria).toEqual(["报告分子、分母和比例"]);
  });

  it("downgrades legacy structured assertions until schema grounding", () => {
    const requirements = parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [{
        kind: "metric",
        description: "计算验证期订单数",
        acceptanceCriteria: ["包含完整结束日期"],
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
          }],
          resultChecks: [{ kind: "non_empty", required: true }],
          claimValues: [{ name: "order_count", field: "order_count", required: true, unit: "orders" }]
        }]
      }]
    }));

    expect(requirements[0]?.assertions).toEqual([expect.objectContaining({
      id: "R1.A1",
      requirementId: "R1",
      kind: "manual",
      sourceTables: [],
      dimensions: [],
      sqlConstraints: [],
      resultChecks: [],
      claimValues: []
    })]);
  });

  it("does not trust physical identifiers extracted before schema grounding", () => {
    const requirements = parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [{
        kind: "metric",
        description: "获取2023年订单记录数及日期范围",
        acceptanceCriteria: ["报告订单数和日期边界"],
        assertions: [{
          kind: "metric",
          description: "订单数与日期范围",
          sourceTables: ["orders"],
          sqlConstraints: [
            { kind: "source", table: "orders" },
            {
              kind: "time_range",
              column: "order_date",
              start: "2023-01-01",
              end: "2023-12-31",
              endInclusive: true
            }
          ],
          resultChecks: [{ kind: "row_count", required: true, min: 1, max: 1 }],
          claimValues: [{ name: "order_count", field: "order_count", required: true }]
        }]
      }]
    }));

    expect(requirements[0]?.assertions).toEqual([
      expect.objectContaining({
        id: "R1.A1",
        kind: "manual",
        sourceTables: [],
        dimensions: [],
        sqlConstraints: [],
        resultChecks: [],
        claimValues: []
      })
    ]);
  });

  it("marks requirements without structured assertions as manual", () => {
    const requirements = parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [{
        kind: "decision",
        description: "判断策略是否合理",
        acceptanceCriteria: ["给出结论"]
      }]
    }));

    expect(requirements[0]?.assertions).toMatchObject([{
      id: "R1.A1",
      kind: "manual",
      description: "判断策略是否合理"
    }]);
  });

  it("rejects unsupported requirement kinds", () => {
    expect(() => parseAnalysisRequirementExtractionText(JSON.stringify({
      requirements: [{ kind: "workflow", description: "do work", acceptanceCriteria: [] }]
    }))).toThrow();
  });

  it("creates a conservative evidence-backed fallback requirement", () => {
    const requirements = createFallbackAnalysisRequirements("比较两个策略并给出建议");

    expect(requirements).toMatchObject([{
      id: "R1",
      kind: "validation",
      description: "比较两个策略并给出建议",
      acceptanceCriteria: ["回答请求中的全部可量化问题，并为结论绑定审计证据"]
    }]);
  });

  it("builds a tool-free extraction prompt that excludes protocol core requirements", () => {
    const prompt = createAnalysisRequirementExtractionPrompt("检查利润公式并比较两个方案");

    expect(prompt).toContain("检查利润公式并比较两个方案");
    expect(prompt).toContain("不要重复 schema inspection");
    expect(prompt).toContain("不要把输出格式");
    expect(prompt).toContain("只返回一个 JSON 对象");
    expect(prompt).toContain("尚未检查物理 schema");
    expect(prompt).toContain("不要输出 assertions");
  });
});
