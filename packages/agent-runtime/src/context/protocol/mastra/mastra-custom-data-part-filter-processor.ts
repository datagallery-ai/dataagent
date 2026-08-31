import type { ProcessOutputStreamArgs, Processor } from "@mastra/core/processors";

export class MastraCustomDataPartFilterProcessor implements Processor<"custom-data-part-filter"> {
  readonly id = "custom-data-part-filter";
  readonly name = "Custom Data Part Filter";
  readonly processDataParts = true;

  async processOutputStream(args: ProcessOutputStreamArgs): Promise<ProcessOutputStreamArgs["part"] | null> {
    if (isCustomDataPart(args.part)) {
      return null;
    }
    return args.part;
  }
}

const isCustomDataPart = (part: unknown): boolean =>
  isRecord(part) && typeof part.type === "string" && part.type.startsWith("data-");

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
