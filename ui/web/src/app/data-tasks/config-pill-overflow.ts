import type { PerRunMentionKind } from "./data-task-state";

export const CONFIG_PILL_GAP_PX = 6;
export const CONFIG_OVERFLOW_BUTTON_WIDTH_PX = 36;
/** Extra slack so subpixel/font differences do not trigger overflow early. */
export const CONFIG_PILL_OVERFLOW_TOLERANCE_PX = 12;

/** Fallback pill widths when DOM measurement is not yet available. */
export const CONFIG_PILL_ESTIMATED_WIDTHS: Record<PerRunMentionKind, number> = {
  db: 90,
  kb: 90,
  mcp: 95,
  skill: 106,
};

function sumWidths(widths: number[], gap: number): number {
  if (widths.length === 0) return 0;
  return widths.reduce(
    (total, width, index) => total + width + (index > 0 ? gap : 0),
    0,
  );
}

/**
 * Decide how many pills stay inline before the rest move into the overflow menu.
 * Reserves space for the "..." control whenever not all pills fit on one row.
 */
export function resolveVisibleConfigPillCount(
  availableWidth: number,
  pillWidths: number[],
  gap = CONFIG_PILL_GAP_PX,
  overflowButtonWidth = CONFIG_OVERFLOW_BUTTON_WIDTH_PX,
  tolerance = CONFIG_PILL_OVERFLOW_TOLERANCE_PX,
): number {
  if (pillWidths.length === 0) return 0;
  if (availableWidth <= 0) return pillWidths.length;

  const budget = availableWidth + tolerance;
  const allVisibleWidth = sumWidths(pillWidths, gap);
  if (allVisibleWidth <= budget) {
    return pillWidths.length;
  }

  for (let visibleCount = pillWidths.length - 1; visibleCount >= 0; visibleCount -= 1) {
    const visibleWidth = sumWidths(pillWidths.slice(0, visibleCount), gap);
    const reserved =
      visibleWidth +
      (visibleCount > 0 ? gap : 0) +
      overflowButtonWidth;
    if (reserved <= budget) {
      return visibleCount;
    }
  }

  return 0;
}

export function hasCompleteConfigPillMeasurements(
  kinds: PerRunMentionKind[],
  measuredWidths: Partial<Record<PerRunMentionKind, number>>,
): boolean {
  return kinds.every(
    (kind) =>
      typeof measuredWidths[kind] === "number" && measuredWidths[kind]! > 0,
  );
}

export function splitConfigPillsByWidth(
  kinds: PerRunMentionKind[],
  availableWidth: number,
  measuredWidths: Partial<Record<PerRunMentionKind, number>> = {},
): {
  visible: PerRunMentionKind[];
  overflow: PerRunMentionKind[];
} {
  if (availableWidth <= 0 || !hasCompleteConfigPillMeasurements(kinds, measuredWidths)) {
    return { visible: kinds, overflow: [] };
  }

  const pillWidths = kinds.map((kind) => measuredWidths[kind]!);
  const visibleCount = resolveVisibleConfigPillCount(availableWidth, pillWidths);
  return {
    visible: kinds.slice(0, visibleCount),
    overflow: kinds.slice(visibleCount),
  };
}
