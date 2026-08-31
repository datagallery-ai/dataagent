import type { ContextItem } from "../inventory/context-item.js";
import type { ContextPackage } from "../inventory/context-package.js";
import {
  contextItemDedupeKeys,
  contextItemExclusivityKey,
  contextItemOverlapKeys,
  contextItemSourceKind,
  contextItemSourceOwner,
  isShadowContextItem
} from "../inventory/context-source-metadata.js";

export type ContextSourcePolicyDecision = {
  affectedGroupIds: string[];
  affectedItemIds: string[];
  reason:
    | "cross_source_overlap_flagged"
    | "duplicate_exact_source_omitted"
    | "non_authoritative_projection_omitted"
    | "shadow_source_not_model_visible";
  strategyId: "context-source-policy";
};

export type ContextSourcePolicyResult = {
  decisions: ContextSourcePolicyDecision[];
  items: ContextItem[];
};

export const isContextSourceOmissionDecision = (decision: ContextSourcePolicyDecision): boolean =>
  decision.reason === "duplicate_exact_source_omitted" ||
  decision.reason === "non_authoritative_projection_omitted" ||
  decision.reason === "shadow_source_not_model_visible";

export type ContextSourcePolicyOptions = {
  authorityOrder?: Record<string, string[]>;
};

export class ContextSourcePolicy {
  private readonly authorityOrder: Record<string, string[]>;

  constructor(options: ContextSourcePolicyOptions = {}) {
    this.authorityOrder = {
      ...(options.authorityOrder ?? {})
    };
  }

  applyPackage(contextPackage: ContextPackage): ContextSourcePolicyResult {
    return this.applyItems(sourcePolicyItemsFromPackage(contextPackage));
  }

  private applyItems(items: ContextItem[]): ContextSourcePolicyResult {
    const overlapDecisions = this.flagOverlappingItems(items);
    const shadowFiltered = this.omitShadowItems(items);
    const authorityFiltered = this.omitNonAuthoritativeItems(shadowFiltered.items);
    const dedupeFiltered = this.omitDuplicateItems(authorityFiltered.items);

    return {
      decisions: [
        ...overlapDecisions,
        ...shadowFiltered.decisions,
        ...authorityFiltered.decisions,
        ...dedupeFiltered.decisions
      ],
      items: dedupeFiltered.items
    };
  }

  private flagOverlappingItems(items: ContextItem[]): ContextSourcePolicyDecision[] {
    const byKey = groupBy(
      items.flatMap((item) => contextItemOverlapKeys(item).map((key) => ({ item, key }))),
      (entry) => entry.key
    );
    const decisions: ContextSourcePolicyDecision[] = [];

    for (const entries of byKey.values()) {
      const uniqueItems = uniqueBy(entries.map((entry) => entry.item), (item) => item.id);
      const sourceKinds = new Set(uniqueItems.map(contextItemSourceKind).filter(isString));
      if (uniqueItems.length > 1 && sourceKinds.size > 1) {
        decisions.push({
          affectedGroupIds: unique(uniqueItems.map((item) => item.groupId)),
          affectedItemIds: unique(uniqueItems.map((item) => item.id)),
          reason: "cross_source_overlap_flagged",
          strategyId: "context-source-policy"
        });
      }
    }

    return decisions;
  }

  private omitShadowItems(items: ContextItem[]): ContextSourcePolicyResult {
    const kept = items.filter((item) => !isShadowContextItem(item));
    const omitted = items.filter(isShadowContextItem);
    return {
      decisions: omitted.map((item) => createDecision(item, "shadow_source_not_model_visible")),
      items: kept
    };
  }

  private omitNonAuthoritativeItems(items: ContextItem[]): ContextSourcePolicyResult {
    const byKey = groupBy(items, (item) => contextItemExclusivityKey(item) ?? item.id);
    const kept: ContextItem[] = [];
    const decisions: ContextSourcePolicyDecision[] = [];

    for (const groupItems of byKey.values()) {
      const selected = selectAuthoritativeItem(groupItems, this.authorityOrder);
      kept.push(selected);
      groupItems
        .filter((item) => item.id !== selected.id)
        .forEach((item) => decisions.push(createDecision(item, "non_authoritative_projection_omitted")));
    }

    return { decisions, items: kept };
  }

  private omitDuplicateItems(items: ContextItem[]): ContextSourcePolicyResult {
    const kept: ContextItem[] = [];
    const decisions: ContextSourcePolicyDecision[] = [];
    const seen = new Set<string>();

    for (const item of items) {
      const keys = contextItemDedupeKeys(item);
      const duplicate = keys.some((key) => seen.has(key));
      if (duplicate) {
        decisions.push(createDecision(item, "duplicate_exact_source_omitted"));
        continue;
      }
      keys.forEach((key) => seen.add(key));
      kept.push(item);
    }

    return { decisions, items: kept };
  }
}

const selectAuthoritativeItem = (
  items: ContextItem[],
  authorityOrder: Record<string, string[]>
): ContextItem => [...items].sort((left, right) =>
  ownerRank(left, authorityOrder) - ownerRank(right, authorityOrder)
  || right.priority - left.priority
  || right.createdAt.localeCompare(left.createdAt)
  || left.id.localeCompare(right.id)
)[0] ?? requireFirstItem(items);

const requireFirstItem = (items: ContextItem[]): ContextItem => {
  const item = items[0];
  if (!item) {
    throw new Error("CONTEXT_SOURCE_POLICY_EMPTY_GROUP");
  }
  return item;
};

const ownerRank = (item: ContextItem, authorityOrder: Record<string, string[]>): number => {
  const sourceKind = contextItemSourceKind(item);
  const sourceOwner = contextItemSourceOwner(item);
  const owners = sourceKind ? authorityOrder[sourceKind] ?? [] : [];
  const index = sourceOwner ? owners.indexOf(sourceOwner) : -1;
  return index >= 0 ? index : owners.length + 1;
};

const createDecision = (
  item: ContextItem,
  reason: ContextSourcePolicyDecision["reason"]
): ContextSourcePolicyDecision => ({
  affectedGroupIds: [item.groupId],
  affectedItemIds: [item.id],
  reason,
  strategyId: "context-source-policy"
});

const groupBy = <T>(items: T[], keyOf: (item: T) => string): Map<string, T[]> => {
  const result = new Map<string, T[]>();
  for (const item of items) {
    const key = keyOf(item);
    result.set(key, [...(result.get(key) ?? []), item]);
  }
  return result;
};

const uniqueBy = <T>(items: T[], keyOf: (item: T) => string): T[] => {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = keyOf(item);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
};

const unique = (values: string[]): string[] => [...new Set(values)].sort((left, right) => left.localeCompare(right));

const isString = (value: unknown): value is string => typeof value === "string";

const sourcePolicyItemsFromPackage = (contextPackage: ContextPackage): ContextItem[] => {
  const sourceItemIds = new Set(
    contextPackage.groups
      .filter((group) => group.kind === "source")
      .flatMap((group) => group.itemIds)
  );
  return contextPackage.items.filter((item) =>
    sourceItemIds.has(item.id)
    && item.visibility === "model"
    && item.metadata.messageKind !== "message"
  );
};
