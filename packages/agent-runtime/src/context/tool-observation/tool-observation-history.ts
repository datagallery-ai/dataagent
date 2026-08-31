import type { ContextPackage } from "../inventory/context-package.js";
import type { ContextItem } from "../inventory/context-item.js";

export const toolObservationHistoryItemsFromPackage = (contextPackage: ContextPackage): ContextItem[] =>
  contextPackage.items.map((item) => {
    if (item.visibility !== "model") {
      return item;
    }

    return {
      ...item,
      visibility: "reference",
      retention: "reference",
      metadata: {
        ...item.metadata,
        groupKind: "reference"
      }
    };
  });
