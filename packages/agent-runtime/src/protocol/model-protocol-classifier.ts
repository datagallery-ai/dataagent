import { Agent } from "@mastra/core/agent";
import type { ModelProvider } from "@datafoundry/providers";
import { z } from "zod";

import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";
import type { ProtocolClassifier, ProtocolIdentity } from "./protocol-router.js";

const classificationSchema = z.object({
  protocolId: z.string().min(1),
  protocolVersion: z.string().min(1),
  confidence: z.number().min(0).max(1),
  reasonCodes: z.array(z.string().regex(/^[A-Z][A-Z0-9_]*$/u)).max(4)
}).strict();

/** Build the constrained prompt consumed by the protocol-only classifier. */
export const createProtocolClassificationPrompt = (input: {
  candidates: ProtocolIdentity[];
  value: unknown;
}): string => {
  const value = isRecord(input.value) ? input.value : {};
  const fields: string[] = [];
  if (typeof value.userText === "string") {
    fields.push(`当前用户查询: ${value.userText}`);
  }
  if (typeof value.previousQuery === "string") {
    fields.push(`上一轮用户查询: ${value.previousQuery}`);
  }
  if (isRecord(value.previousProtocol)) {
    const prev = value.previousProtocol;
    const parts: string[] = [];
    if (typeof prev.protocolId === "string") parts.push(prev.protocolId);
    if (typeof prev.terminalStatus === "string") parts.push(`终态=${prev.terminalStatus}`);
    if (parts.length > 0) fields.push(`上一轮协议: ${parts.join(" ")}`);
  }
  if (Array.isArray(value.selectedSkillIds) && value.selectedSkillIds.length > 0) {
    fields.push(`已选 skill: ${value.selectedSkillIds.join(", ")}`);
  }
  if (typeof value.selectedDatasourceId === "string") {
    fields.push(`已选数据源: ${value.selectedDatasourceId}`);
  }
  return [
    "你是协议路由分类器，不是执行任务的 Agent。",
    "只能选择候选集合中的协议，不得发明协议或调用工具。",
    "data-analysis 用于需要数据源、schema、SQL、指标、统计或数据结论的任务。",
    "general-task 用于日常问答、解释、总结、文件、知识检索和普通协作任务。",
    "当当前查询是弱后续(如 继续、重试、再试、再来一次、try again 等)且上一轮使用的是 data-analysis 时，",
    "应倾向于延续 data-analysis，除非用户明确切换了任务主题。",
    `候选集合: ${input.candidates.map((item) => `${item.protocolId}@${item.protocolVersion}`).join(", ")}`,
    ...(fields.length > 0 ? ["分类输入:", ...fields] : [`分类输入: ${JSON.stringify(input.value)}`]),
    "只返回一个 JSON 对象，不要 Markdown。字段为 protocolId、protocolVersion、confidence、reasonCodes。",
    '格式示例: {"protocolId":"data-analysis","protocolVersion":"1","confidence":0.91,"reasonCodes":["INHERITED_PRIOR_PROTOCOL"]}',
    "reasonCodes 只能使用大写英文与下划线。"
  ].join("\n");
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Parse model text into the strict classifier contract without trusting provider-specific JSON modes. */
export const parseProtocolClassificationText = (text: string): z.infer<typeof classificationSchema> => {
  const trimmed = text.trim();
  const unfenced = trimmed.startsWith("```")
    ? trimmed.replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "")
    : trimmed;
  return classificationSchema.parse(JSON.parse(unfenced) as unknown);
};

/** Create a tool-free structured classifier backed by the configured run model. */
export const createModelProtocolClassifier = (
  provider: Exclude<ModelProvider, { kind: "mock" }>
): ProtocolClassifier => {
  const agent = new Agent({
    id: "protocol-route-classifier",
    name: "Protocol Route Classifier",
    instructions: "Classify only. Never answer the user's task.",
    model: provider.model as never
  });
  return async (input) => {
    const output = await agent.generate(createProtocolClassificationPrompt(input), {
      maxSteps: AGENT_RUNTIME_LIMITS.modelHelperMaxSteps,
      modelSettings: { maxOutputTokens: AGENT_RUNTIME_LIMITS.protocolClassifierMaxOutputTokens, temperature: 0 }
    });
    return parseProtocolClassificationText(output.text);
  };
};
