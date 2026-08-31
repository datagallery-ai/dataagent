import { describe, expect, it } from "vitest";
import {
  canDockRightPanel,
  chatPaneClassName,
  clampLeftPanelWidth,
  clampRightPanelWidth,
  fixedGridColumn,
  getChatInputReservedWidth,
  getMinimumWorkspaceWidth,
  getRequiredWorkspaceWidth,
  getWorkspaceGridTemplateColumns,
  LEFT_PANEL_DEFAULT_WIDTH,
  LEFT_PANEL_MAX_WIDTH,
  LEFT_PANEL_MIN_WIDTH,
  maxDockableRightPanelWidth,
  RIGHT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_MAX_WIDTH,
  RIGHT_PANEL_MIN_WIDTH,
  resolveResponsiveSidebars,
  resolveSidebarExpandPreferences,
  CHAT_MIN_WIDTH,
  LEFT_PANEL_WIDTH_COLLAPSED,
} from "../workspace-layout";

describe("workspace layout", () => {
  it("uses fixed minmax columns for left and right panels", () => {
    expect(
      getWorkspaceGridTemplateColumns({
        isConfigPanelOpen: false,
        isRightPanelOpen: true,
        sidebarCollapsed: false,
        rightPanelWidth: 400,
      }),
    ).toBe(
      `${fixedGridColumn(LEFT_PANEL_DEFAULT_WIDTH)} minmax(${CHAT_MIN_WIDTH}px, 1fr) ${fixedGridColumn(400)}`,
    );
  });

  it("falls back to the default width when none is supplied", () => {
    expect(
      getWorkspaceGridTemplateColumns({
        isConfigPanelOpen: false,
        isRightPanelOpen: true,
        sidebarCollapsed: false,
      }),
    ).toContain(fixedGridColumn(RIGHT_PANEL_DEFAULT_WIDTH));
  });

  it("collapses the right track to zero (keeping three tracks) when the console is closed", () => {
    // Track count stays constant so `grid-template-columns` transitions
    // smoothly instead of leaving an empty reserved column mid-animation.
    expect(
      getWorkspaceGridTemplateColumns({
        isConfigPanelOpen: false,
        isRightPanelOpen: false,
        sidebarCollapsed: false,
      }),
    ).toBe(
      `${fixedGridColumn(LEFT_PANEL_DEFAULT_WIDTH)} minmax(${CHAT_MIN_WIDTH}px, 1fr) ${fixedGridColumn(0)}`,
    );
  });

  it("collapses the right track to zero when a config panel takes over the middle column", () => {
    expect(
      getWorkspaceGridTemplateColumns({
        isConfigPanelOpen: true,
        isRightPanelOpen: true,
        sidebarCollapsed: false,
      }),
    ).toBe(
      `${fixedGridColumn(LEFT_PANEL_DEFAULT_WIDTH)} minmax(${CHAT_MIN_WIDTH}px, 1fr) ${fixedGridColumn(0)}`,
    );
  });

  it("centers message rows within the content column max-width", () => {
    expect(chatPaneClassName).toContain("w-full");
    expect(chatPaneClassName).toContain("[&_.copilotKitMessage]:mx-auto");
    expect(chatPaneClassName).toContain("[&_.copilotKitMessage]:max-w-[760px]");
  });
});

describe("clampLeftPanelWidth", () => {
  it("never drops below the minimum width", () => {
    expect(clampLeftPanelWidth(100)).toBe(LEFT_PANEL_MIN_WIDTH);
  });

  it("caps at the absolute maximum", () => {
    expect(clampLeftPanelWidth(400)).toBe(LEFT_PANEL_MAX_WIDTH);
  });

  it("keeps a requested width within bounds", () => {
    expect(clampLeftPanelWidth(240)).toBe(240);
  });
});

describe("clampRightPanelWidth", () => {
  it("never drops below the minimum width", () => {
    expect(clampRightPanelWidth(100)).toBe(RIGHT_PANEL_MIN_WIDTH);
  });

  it("does not impose a fixed upper cap (viewport drives the max)", () => {
    expect(clampRightPanelWidth(900)).toBe(900);
  });

  it("keeps a requested width within bounds", () => {
    expect(clampRightPanelWidth(420)).toBe(420);
  });

  it("does not shrink width on a narrow viewport", () => {
    expect(clampRightPanelWidth(360)).toBe(360);
  });
});

describe("resolveResponsiveSidebars", () => {
  it("keeps user preferences when the viewport fits the preferred chat input", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 1500,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: true,
    });
  });

  it("keeps the left sidebar expanded when the docked console still fits the chat minimum", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 1200,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: true,
    });
  });

  it("keeps the left sidebar expanded before closing the console for preferred-width pressure", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 1300,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: true,
    });
  });

  it("closes the right panel before folding the left sidebar", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 900,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: false,
    });
  });

  it("keeps the left panel on narrow viewports when closing the right panel is enough", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 700,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: false,
    });
  });

  it("collapses the left panel only when the minimum chat column would still overflow", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 500,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: true,
      rightPanelOpen: false,
    });
  });

  it("restores user preferences when the viewport becomes wide enough again", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 1500,
        userSidebarCollapsed: false,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: true,
    });
  });

  it("keeps an explicitly expanded left panel when only the chat input preference overflows", () => {
    expect(
      resolveResponsiveSidebars({
        viewportWidth: 1000,
        userSidebarCollapsed: false,
        userRightPanelOpen: false,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      sidebarCollapsed: false,
      rightPanelOpen: false,
    });
  });
});

describe("getRequiredWorkspaceWidth", () => {
  it("sums fixed side widths plus the preferred chat input reservation", () => {
    expect(
      getRequiredWorkspaceWidth({
        sidebarCollapsed: false,
        rightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toBe(LEFT_PANEL_DEFAULT_WIDTH + 360 + getChatInputReservedWidth());
  });
});

describe("getMinimumWorkspaceWidth", () => {
  it("sums fixed side widths plus the chat column minimum", () => {
    expect(
      getMinimumWorkspaceWidth({
        sidebarCollapsed: false,
        rightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toBe(LEFT_PANEL_DEFAULT_WIDTH + 360 + CHAT_MIN_WIDTH);
  });
});

describe("canDockRightPanel", () => {
  it("returns true when the viewport fits collapsed left + right + chat minimum", () => {
    const minWidth =
      LEFT_PANEL_WIDTH_COLLAPSED + RIGHT_PANEL_MIN_WIDTH + CHAT_MIN_WIDTH;
    expect(
      canDockRightPanel({
        viewportWidth: minWidth,
        rightPanelWidth: RIGHT_PANEL_MIN_WIDTH,
      }),
    ).toBe(true);
  });

  it("returns false when the viewport is narrower than the dock minimum", () => {
    const minWidth =
      LEFT_PANEL_WIDTH_COLLAPSED + RIGHT_PANEL_MIN_WIDTH + CHAT_MIN_WIDTH;
    expect(
      canDockRightPanel({
        viewportWidth: minWidth - 1,
        rightPanelWidth: RIGHT_PANEL_MIN_WIDTH,
      }),
    ).toBe(false);
  });

  it("keeps a max-width panel dockable once the chat is squeezed to its minimum", () => {
    // Regression: dragging to RIGHT_PANEL_MAX_WIDTH previously undocked the
    // panel because docking reserved the *preferred* chat width, not the min.
    const viewport =
      LEFT_PANEL_WIDTH_COLLAPSED + RIGHT_PANEL_MAX_WIDTH + CHAT_MIN_WIDTH;
    expect(
      canDockRightPanel({
        viewportWidth: viewport,
        rightPanelWidth: RIGHT_PANEL_MAX_WIDTH,
      }),
    ).toBe(true);
  });

  it("returns true for unknown viewport width (SSR/hydration)", () => {
    expect(
      canDockRightPanel({
        viewportWidth: 0,
        rightPanelWidth: RIGHT_PANEL_DEFAULT_WIDTH,
      }),
    ).toBe(true);
  });
});

describe("maxDockableRightPanelWidth", () => {
  it("returns the fallback max when the viewport is unknown", () => {
    expect(maxDockableRightPanelWidth(0)).toBe(RIGHT_PANEL_MAX_WIDTH);
  });

  it("caps the width so the resulting panel stays dockable", () => {
    // A viewport that cannot fit a full-width panel: the returned width must be
    // small enough that canDockRightPanel is satisfied.
    const viewport =
      LEFT_PANEL_WIDTH_COLLAPSED + RIGHT_PANEL_MAX_WIDTH + CHAT_MIN_WIDTH - 120;
    const capped = maxDockableRightPanelWidth(viewport);
    expect(capped).toBeLessThan(RIGHT_PANEL_MAX_WIDTH);
    expect(
      canDockRightPanel({ viewportWidth: viewport, rightPanelWidth: capped }),
    ).toBe(true);
  });

  it("never returns below the absolute minimum width", () => {
    expect(maxDockableRightPanelWidth(100)).toBe(RIGHT_PANEL_MIN_WIDTH);
  });

  it("keeps the chat column at its absolute minimum regardless of viewport size", () => {
    // The chat minimum must be a constant: the max panel width grows 1:1 with
    // the viewport so that viewport - left - maxRight === CHAT_MIN_WIDTH.
    for (const viewport of [1200, 1920, 2560, 4000]) {
      const left = LEFT_PANEL_WIDTH_COLLAPSED;
      const maxRight = maxDockableRightPanelWidth(viewport, left);
      expect(viewport - left - maxRight).toBe(CHAT_MIN_WIDTH);
    }
  });

  it("lets the panel grow past the fallback max on a wide viewport", () => {
    const left = LEFT_PANEL_WIDTH_COLLAPSED;
    const viewport = 4000;
    expect(maxDockableRightPanelWidth(viewport, left)).toBe(
      viewport - left - CHAT_MIN_WIDTH,
    );
    expect(maxDockableRightPanelWidth(viewport, left)).toBeGreaterThan(
      RIGHT_PANEL_MAX_WIDTH,
    );
  });
});

describe("resolveSidebarExpandPreferences", () => {
  it("keeps right panel open when the viewport fits expanded left + right", () => {
    expect(
      resolveSidebarExpandPreferences({
        viewportWidth: 1500,
        userRightPanelOpen: true,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      userSidebarCollapsed: false,
      userRightPanelOpen: true,
    });
  });

  it("never closes the right panel when expanding left, even on a narrow viewport", () => {
    // Regression: expanding the left sidebar used to close the right console
    // when the expanded layout overflowed. Expansion is an explicit user
    // action and must preserve the right panel; the centered chat column
    // absorbs the width change instead.
    expect(
      resolveSidebarExpandPreferences({
        viewportWidth: 900,
        userRightPanelOpen: true,
        rightPanelWidth: RIGHT_PANEL_MAX_WIDTH,
      }),
    ).toEqual({
      userSidebarCollapsed: false,
      userRightPanelOpen: true,
    });
  });

  it("keeps the right panel closed when it was already closed", () => {
    expect(
      resolveSidebarExpandPreferences({
        viewportWidth: 1500,
        userRightPanelOpen: false,
        rightPanelWidth: 360,
      }),
    ).toEqual({
      userSidebarCollapsed: false,
      userRightPanelOpen: false,
    });
  });
});
