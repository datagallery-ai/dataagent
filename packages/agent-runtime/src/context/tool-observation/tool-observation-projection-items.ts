import type {
  ArtifactRef,
  AuditRef,
  ContextPackage,
  ContextTruncation
} from "../inventory/context-package.js";
import {
  createContextItem,
  type ContextItem,
  type ContextItemVisibility
} from "../inventory/context-item.js";
import { createContextSourceMetadata } from "../inventory/context-source-metadata.js";

export type ToolObservationProjection = {
  activity: unknown;
  artifactRefs: ArtifactRef[];
  auditRefs: AuditRef[];
  model: unknown;
  truncation: ContextTruncation[];
};

export const toolObservationModelFromPackage = (contextPackage: ContextPackage): unknown =>
  collapseToolObservationItems(contextPackage, "model");

export const toolObservationActivityFromPackage = (contextPackage: ContextPackage): unknown =>
  collapseToolObservationItems(contextPackage, "activity");

export const toolObservationProjectionToItems = (
  projection: ToolObservationProjection,
  sourceType: string,
  priority: number
): ContextItem[] => {
  const groupId = `${sourceType}-observation`;
  const items: ContextItem[] = [
    createProjectedItem(`${sourceType}-model`, sourceType, groupId, "model", priority, projection.model),
    createProjectedItem(`${sourceType}-activity`, sourceType, groupId, "activity", priority, projection.activity)
  ];

  projection.artifactRefs.forEach((ref, index) => {
    items.push(
      createProjectedItem(`${sourceType}-artifact-${index}`, sourceType, groupId, "artifact-ref", priority, ref)
    );
  });
  projection.auditRefs.forEach((ref, index) => {
    items.push(createProjectedItem(`${sourceType}-audit-${index}`, sourceType, groupId, "audit-ref", priority, ref));
  });
  projection.truncation.forEach((entry, index) => {
    items.push(
      createProjectedItem(`${sourceType}-truncation-${index}`, sourceType, groupId, "truncation", priority, entry)
    );
  });

  return items;
};

const createProjectedItem = (
  id: string,
  sourceType: string,
  groupId: string,
  visibility: ContextItemVisibility,
  priority: number,
  content: unknown
): ContextItem => createContextItem({
  id,
  sourceType,
  sourceId: sourceType,
  groupId,
  visibility,
  trust: "tool",
  retention: visibility === "model" ? "supporting" : "reference",
  priority,
  content,
  metadata: createContextSourceMetadata({
    dedupeKeys: [`tool-observation:${sourceType}`],
    exclusivityKey: `tool-observation:${sourceType}`,
    sourceKind: "tool-observation",
    sourceOwner: "tool"
  }, { groupKind: visibility === "model" ? "tool-exchange" : "reference" })
});

const collapseToolObservationItems = (
  contextPackage: ContextPackage,
  visibility: ContextItemVisibility
): unknown => {
  const contents = contextPackage.items
    .filter((item) => item.visibility === visibility)
    .map((item) => item.content);

  if (contents.length === 0) {
    return null;
  }

  return contents.length === 1 ? contents[0] : contents;
};
