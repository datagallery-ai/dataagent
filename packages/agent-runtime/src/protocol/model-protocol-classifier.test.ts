import { describe, expect, it } from "vitest";

import {
  createProtocolClassificationPrompt,
  parseProtocolClassificationText
} from "./model-protocol-classifier.js";

describe("createProtocolClassificationPrompt", () => {
  it("constrains classification to the router-provided candidates", () => {
    const prompt = createProtocolClassificationPrompt({
      candidates: [
        { protocolId: "general-task", protocolVersion: "1" },
        { protocolId: "data-analysis", protocolVersion: "1" }
      ],
      value: { userText: "比较订单趋势" }
    });

    expect(prompt).toContain("general-task@1");
    expect(prompt).toContain("data-analysis@1");
    expect(prompt).toContain("比较订单趋势");
    expect(prompt).toContain("只能选择候选集合中的协议");
    expect(prompt).toContain("INHERITED_PRIOR_PROTOCOL");
  });

  it("surfaces routing context fields so the classifier can inherit prior intent", () => {
    const prompt = createProtocolClassificationPrompt({
      candidates: [
        { protocolId: "general-task", protocolVersion: "1" },
        { protocolId: "data-analysis", protocolVersion: "1" }
      ],
      value: {
        userText: "再次尝试",
        previousQuery: "帮我分析当前数据",
        previousProtocol: { protocolId: "data-analysis", protocolVersion: "1", terminalStatus: "completed" },
        selectedSkillIds: ["data-analysis"],
        selectedDatasourceId: "orders-db"
      }
    });

    expect(prompt).toContain("当前用户查询: 再次尝试");
    expect(prompt).toContain("上一轮用户查询: 帮我分析当前数据");
    expect(prompt).toContain("上一轮协议: data-analysis 终态=completed");
    expect(prompt).toContain("已选 skill: data-analysis");
    expect(prompt).toContain("已选数据源: orders-db");
    expect(prompt).toContain("弱后续");
    expect(prompt).toContain("延续 data-analysis");
  });

  it("strictly parses fenced JSON returned by compatible models", () => {
    expect(parseProtocolClassificationText(`\`\`\`json
{"protocolId":"general-task","protocolVersion":"1","confidence":0.9,"reasonCodes":["GENERAL_EXPLANATION"]}
\`\`\``)).toEqual({
      protocolId: "general-task",
      protocolVersion: "1",
      confidence: 0.9,
      reasonCodes: ["GENERAL_EXPLANATION"]
    });
  });
});
