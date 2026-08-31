import { createHash } from "node:crypto";

export type ContextItemVisibility = "model" | "activity" | "artifact-ref" | "audit-ref" | "truncation" | "reference";
export type ContextTrust = "runtime" | "tool" | "memory" | "knowledge" | "untrusted-client";
export type ContextRetention = "mandatory" | "active" | "supporting" | "historical" | "reference";

export type ContextItem = {
  id: string;
  sourceType: string;
  sourceId: string;
  groupId: string;
  visibility: ContextItemVisibility;
  trust: ContextTrust;
  retention: ContextRetention;
  priority: number;
  content: unknown;
  contentHash: string;
  createdAt: string;
  metadata: Record<string, unknown>;
};

export type CreateContextItemInput = Omit<ContextItem, "contentHash" | "createdAt"> & {
  contentHash?: string;
  createdAt?: string;
};

export const createContextItem = (input: CreateContextItemInput): ContextItem => ({
  ...input,
  contentHash: input.contentHash ?? hashContextContent(input.content),
  createdAt: input.createdAt ?? new Date().toISOString()
});

export const hashContextContent = (content: unknown): string =>
  createHash("sha256").update(stableSerialize(content)).digest("hex");

const stableSerialize = (value: unknown): string => JSON.stringify(sortValue(value));

const sortValue = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    return value.map(sortValue);
  }
  if (!isRecord(value)) {
    return value;
  }

  return Object.fromEntries(
    Object.entries(value)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, entry]) => [key, sortValue(entry)])
  );
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
