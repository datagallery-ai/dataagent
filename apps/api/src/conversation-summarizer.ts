import { Agent } from "@mastra/core/agent";
import type { ConversationMessageRecord, ConversationSummaryRecord } from "@datafoundry/metadata";

import { type ConversationSummarizer, type ConversationSummarizerInput } from "./conversation-memory.js";

export type MastraConversationSummarizerInput = {
  maxOutputTokens?: number | undefined;
  model: unknown;
  temperature?: number | undefined;
};

export class MastraConversationSummarizer implements ConversationSummarizer {
  readonly kind = "mastra-agent";
  private readonly agent: Agent;
  private readonly maxOutputTokens: number;
  private readonly temperature: number;

  constructor(input: MastraConversationSummarizerInput) {
    this.maxOutputTokens = input.maxOutputTokens ?? 900;
    this.temperature = input.temperature ?? 0.1;
    this.agent = new Agent({
      id: "conversation-summarizer",
      name: "Conversation Summarizer",
      instructions: [
        "You maintain compact conversation memory for a data analysis agent.",
        "Summarize only facts, user preferences, decisions, active goals, unresolved questions, and verified results.",
        "Preserve datasource names, table names, field names, SQL intent, artifact references, and user constraints.",
        "Do not invent schema, query results, tool output, credentials, or private environment values.",
        "Return concise Chinese text unless the source messages are primarily in another language.",
        "Do not include XML tags, markdown tables, or implementation commentary."
      ].join("\n"),
      model: input.model as never,
      defaultOptions: {
        maxSteps: 1,
        modelSettings: {
          maxOutputTokens: this.maxOutputTokens,
          temperature: this.temperature
        },
        providerOptions: {
          openai: {
            systemMessageMode: "system"
          }
        }
      }
    });
  }

  async summarize(input: ConversationSummarizerInput): Promise<string> {
    const prompt = buildSummarizerPrompt(input);
    const output = await this.agent.generate(prompt);
    return output.text.trim();
  }
}

export const createMastraConversationSummarizer = (
  input: MastraConversationSummarizerInput
): MastraConversationSummarizer => new MastraConversationSummarizer(input);

const buildSummarizerPrompt = (input: ConversationSummarizerInput): string => {
  const previousSummary = formatPreviousSummary(input.previousSummary);
  const sourceMessages = input.sourceMessages.map(formatSourceMessage).join("\n");
  return [
    `Summarize this conversation memory segment into at most ${input.maxChars} characters.`,
    "The summary will replace the source messages in a future prompt, so keep durable context and omit filler.",
    previousSummary,
    "<source_messages>",
    sourceMessages,
    "</source_messages>"
  ].filter(Boolean).join("\n\n");
};

const formatPreviousSummary = (summary?: ConversationSummaryRecord | undefined): string =>
  summary
    ? [
        `<previous_summary from_position="${summary.from_position}" to_position="${summary.to_position}">`,
        summary.summary_text,
        "</previous_summary>"
      ].join("\n")
    : "";

const formatSourceMessage = (message: ConversationMessageRecord): string =>
  [
    `<message position="${message.position}" role="${message.role}" run_id="${message.run_id}">`,
    message.content_text,
    "</message>"
  ].join("\n");
