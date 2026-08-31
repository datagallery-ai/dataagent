import { Agent } from "@mastra/core/agent";
import type { ModelProvider } from "@datafoundry/providers";
import { z } from "zod";

import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";
import {
  ANALYSIS_REQUIREMENT_KINDS,
  createUserAnalysisRequirements,
  type AnalysisRequirement,
  type AnalysisRequirementDraft
} from "./analysis-requirements.js";
import { analysisAssertionDraftSchema } from "./analysis-contract.js";

const requirementDraftSchema = z.object({
  id: z.string().optional(),
  kind: z.enum(ANALYSIS_REQUIREMENT_KINDS),
  description: z.string().min(1).max(500),
  acceptanceCriteria: z.preprocess(
    (value) => typeof value === "string" ? [value] : value,
    z.array(z.string().min(1).max(300)).max(8)
  ),
  assertions: z.array(analysisAssertionDraftSchema).max(12).optional()
}).strict();

const extractionSchema = z.object({
  requirements: z.array(requirementDraftSchema).min(1).max(16)
}).strict();

export type AnalysisRequirementExtractor = (input: { userText: string }) => Promise<AnalysisRequirement[]>;

/** Build the constrained prompt for user-specific analysis requirements. */
export const createAnalysisRequirementExtractionPrompt = (userText: string): string => [
  "你是数据分析验收条件提取器，不是执行任务的 Agent。",
  "把用户请求拆成可由 SQL 结果和审计证据验证的必答要求。",
  "当前阶段尚未检查物理 schema。不得猜测或输出任何表名、字段名、SQL 过滤条件或聚合字段。",
  "只提取业务目标和验收标准；物理 assertions 会在 schema 与语义解析完成后由独立步骤生成。",
  "不要重复 schema inspection、semantic grounding、read-only validation、result validation 或 evidence binding，",
  "这些由 Protocol 固定提供。不要加入寒暄、过程说明或可选建议。",
  "不要把输出格式、Markdown、文件或下载要求提取为 requirement；它们不属于 SQL 证据验收范围。",
  `用户请求: ${userText}`,
  `kind 只能是: ${ANALYSIS_REQUIREMENT_KINDS.join(", ")}`,
  "只返回一个 JSON 对象，不要 Markdown。字段为 requirements；每项字段只能包含 kind、description 和",
  "acceptanceCriteria。acceptanceCriteria 必须是字符串数组，即使只有一项也要使用数组。",
  "不要输出 assertions、sourceTables、dimensions、sqlConstraints、resultChecks 或 claimValues。",
  "不要生成 id，服务端会分配稳定 ID。"
].join("\n");

/** Parse model output and assign stable server-owned requirement IDs. */
export const parseAnalysisRequirementExtractionText = (text: string): AnalysisRequirement[] => {
  const trimmed = text.trim();
  const unfenced = trimmed.startsWith("```")
    ? trimmed.replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "")
    : trimmed;
  const parsed = extractionSchema.parse(JSON.parse(unfenced) as unknown);
  const drafts: AnalysisRequirementDraft[] = parsed.requirements
    .filter((requirement) => requirement.kind !== "deliverable")
    .map((requirement) => ({
      kind: requirement.kind,
      description: requirement.description,
      acceptanceCriteria: requirement.acceptanceCriteria
    }));
  return createUserAnalysisRequirements(drafts);
};

/** Create a conservative requirement when model extraction cannot produce valid JSON. */
export const createFallbackAnalysisRequirements = (userText: string): AnalysisRequirement[] => createUserAnalysisRequirements([{
  kind: "validation",
  description: userText.trim().slice(0, 500) || "完成用户请求的数据分析",
  acceptanceCriteria: ["回答请求中的全部可量化问题，并为结论绑定审计证据"]
}]);

/** Create a tool-free structured requirement extractor backed by the configured run model. */
export const createModelAnalysisRequirementExtractor = (
  provider: Exclude<ModelProvider, { kind: "mock" }>
): AnalysisRequirementExtractor => {
  const agent = new Agent({
    id: "analysis-requirement-extractor",
    name: "Analysis Requirement Extractor",
    instructions: "Extract verification requirements only. Never answer the user's task.",
    model: provider.model as never
  });
  return async (input) => {
    const basePrompt = createAnalysisRequirementExtractionPrompt(input.userText);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const retryInstruction = attempt === 0
        ? ""
        : "\n上次输出不是合法 JSON。请缩短描述，确保所有字符串闭合，并严格返回紧凑 JSON。";
      const output = await agent.generate(`${basePrompt}${retryInstruction}`, {
        maxSteps: AGENT_RUNTIME_LIMITS.modelHelperMaxSteps,
        modelSettings: {
          maxOutputTokens: AGENT_RUNTIME_LIMITS.requirementExtractorMaxOutputTokens,
          temperature: 0
        }
      });
      try {
        return parseAnalysisRequirementExtractionText(output.text);
      } catch {
        // Retry once before falling back to a conservative server-owned requirement.
      }
    }
    return createFallbackAnalysisRequirements(input.userText);
  };
};
