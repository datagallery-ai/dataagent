import { describe, expect, it } from "vitest";
import {
  CONSOLE_FILE_ASSETS_TAB,
  CONSOLE_PEER_PAGE_ID,
  nextConsoleEscapeAction,
  overviewSectionPlan,
  shouldRevealConsoleForSelection,
} from "../task-console-layout";

describe("task console information architecture", () => {
  it("keeps overview focused on conclusion and progress", () => {
    expect(
      overviewSectionPlan({
        hasToolDistribution: true,
      }),
    ).toEqual([
      { id: "conclusion", collapsible: false },
      { id: "progress", collapsible: false },
      { id: "tool-distribution", collapsible: true },
    ]);
  });

  it("assigns workspace file assets to outputs instead of overview", () => {
    expect(CONSOLE_FILE_ASSETS_TAB).toBe("outputs");
  });

  it("reveals the console peer page for tool/step selection from chat", () => {
    expect(CONSOLE_PEER_PAGE_ID).toBe("console");
    expect(shouldRevealConsoleForSelection({ type: "action" })).toBe(true);
    expect(shouldRevealConsoleForSelection({ type: "toolGroup" })).toBe(true);
    expect(shouldRevealConsoleForSelection({ type: "artifact" })).toBe(false);
    expect(shouldRevealConsoleForSelection(null)).toBe(false);
  });

  it("orders Escape so preview and peer pages dismiss before the drawer", () => {
    expect(
      nextConsoleEscapeAction({
        previewOpen: true,
        onPeerPage: true,
        drawerOpen: true,
      }),
    ).toBe("preview");
    expect(
      nextConsoleEscapeAction({
        previewOpen: false,
        onPeerPage: true,
        drawerOpen: true,
      }),
    ).toBe("peerPage");
    expect(
      nextConsoleEscapeAction({
        previewOpen: false,
        onPeerPage: false,
        drawerOpen: true,
      }),
    ).toBe("drawer");
    expect(
      nextConsoleEscapeAction({
        previewOpen: false,
        onPeerPage: false,
        drawerOpen: false,
      }),
    ).toBeNull();
  });
});
