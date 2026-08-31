import { describe, expect, it } from "vitest";
import {
  CONFIG_PILL_OVERFLOW_TOLERANCE_PX,
  hasCompleteConfigPillMeasurements,
  resolveVisibleConfigPillCount,
  splitConfigPillsByWidth,
} from "../config-pill-overflow";
import { PER_RUN_MENTION_KINDS } from "../data-task-state";

const FOUR_PILL_WIDTHS = [90, 90, 95, 106];

describe("resolveVisibleConfigPillCount", () => {
  it("shows all pills when there is enough width", () => {
    expect(resolveVisibleConfigPillCount(420, FOUR_PILL_WIDTHS)).toBe(4);
  });

  it("applies tolerance before collapsing pills", () => {
    const total = 399;
    expect(
      resolveVisibleConfigPillCount(
        total - CONFIG_PILL_OVERFLOW_TOLERANCE_PX,
        FOUR_PILL_WIDTHS,
      ),
    ).toBe(4);
  });

  it("reserves space for the overflow button when not all pills fit", () => {
    expect(resolveVisibleConfigPillCount(360, FOUR_PILL_WIDTHS)).toBe(3);
  });

  it("can collapse every pill into the overflow menu on very narrow widths", () => {
    expect(resolveVisibleConfigPillCount(30, FOUR_PILL_WIDTHS)).toBe(0);
  });
});

describe("hasCompleteConfigPillMeasurements", () => {
  it("requires every kind to be measured", () => {
    expect(
      hasCompleteConfigPillMeasurements(PER_RUN_MENTION_KINDS, {
        db: 90,
        kb: 90,
      }),
    ).toBe(false);
    expect(
      hasCompleteConfigPillMeasurements(PER_RUN_MENTION_KINDS, {
        db: 90,
        kb: 90,
        mcp: 95,
        skill: 106,
      }),
    ).toBe(true);
  });
});

describe("splitConfigPillsByWidth", () => {
  it("keeps all pills visible until measurements are complete", () => {
    expect(splitConfigPillsByWidth(PER_RUN_MENTION_KINDS, 360)).toEqual({
      visible: PER_RUN_MENTION_KINDS,
      overflow: [],
    });
  });

  it("splits kinds into visible and overflow groups once measured", () => {
    expect(
      splitConfigPillsByWidth(PER_RUN_MENTION_KINDS, 360, {
        db: 90,
        kb: 90,
        mcp: 95,
        skill: 106,
      }),
    ).toEqual({
      visible: ["db", "kb", "mcp"],
      overflow: ["skill"],
    });
  });

  it("uses measured widths for the final split", () => {
    expect(
      splitConfigPillsByWidth(PER_RUN_MENTION_KINDS, 250, {
        db: 70,
        kb: 70,
        mcp: 70,
        skill: 70,
      }),
    ).toEqual({
      visible: ["db", "kb"],
      overflow: ["mcp", "skill"],
    });
  });
});
