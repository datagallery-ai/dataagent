import type { ContextItem } from "./context-item.js";

export type ContextSourceScope = {
  datasourceId?: string;
  sessionId?: string;
  userId?: string;
};

export type ContextSourceMetadata = {
  dedupeKeys?: string[];
  exclusivityKey?: string;
  overlapKeys?: string[];
  scope?: ContextSourceScope;
  shadow?: boolean;
  sourceKind: string;
  sourceOwner: string;
};

export const createContextSourceMetadata = (
  metadata: ContextSourceMetadata,
  extra: Record<string, unknown> = {}
): Record<string, unknown> => ({
  ...extra,
  dedupeKeys: metadata.dedupeKeys ?? [],
  exclusivityKey: metadata.exclusivityKey ?? `${metadata.sourceKind}:${metadata.sourceOwner}`,
  overlapKeys: metadata.overlapKeys ?? [],
  scope: metadata.scope ?? {},
  shadow: metadata.shadow ?? false,
  sourceKind: metadata.sourceKind,
  sourceOwner: metadata.sourceOwner
});

export const contextItemSourceKind = (item: ContextItem): string | undefined =>
  stringMetadata(item, "sourceKind");

export const contextItemSourceOwner = (item: ContextItem): string | undefined =>
  stringMetadata(item, "sourceOwner");

export const contextItemExclusivityKey = (item: ContextItem): string | undefined =>
  stringMetadata(item, "exclusivityKey");

export const contextItemDedupeKeys = (item: ContextItem): string[] => {
  const value = item.metadata.dedupeKeys;
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
};

export const contextItemOverlapKeys = (item: ContextItem): string[] => {
  const value = item.metadata.overlapKeys;
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
};

export const contextItemScope = (item: ContextItem): ContextSourceScope => {
  const scope = item.metadata.scope;
  if (!isRecord(scope)) {
    return {};
  }

  return {
    ...(typeof scope.datasourceId === "string" ? { datasourceId: scope.datasourceId } : {}),
    ...(typeof scope.sessionId === "string" ? { sessionId: scope.sessionId } : {}),
    ...(typeof scope.userId === "string" ? { userId: scope.userId } : {})
  };
};

export const isShadowContextItem = (item: ContextItem): boolean => item.metadata.shadow === true;

const stringMetadata = (item: ContextItem, key: string): string | undefined => {
  const value = item.metadata[key];
  return typeof value === "string" ? value : undefined;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
