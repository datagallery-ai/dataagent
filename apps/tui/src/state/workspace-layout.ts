export const RIGHT_PANEL_MIN_WIDTH = 320;
/**
 * Fallback upper bound for the right panel, used *only* when the viewport width
 * is unknown (SSR / hydration). The effective maximum is otherwise derived from
 * the viewport by {@link maxDockableRightPanelWidth} so the chat column always
 * keeps the same absolute minimum width ({@link CHAT_MIN_WIDTH}) at every screen
 * size — the panel is free to grow past this value on wide viewports.
 */
export const RIGHT_PANEL_MAX_WIDTH = 640;
export const RIGHT_PANEL_DEFAULT_WIDTH = 400;
export const CHAT_MIN_WIDTH = 420;
/**
 * Centered chat content column bounds. Messages and the input flow between the
 * min and max width and stay horizontally centered within the chat column, so
 * opening/closing side panels shifts the centered column instead of reflowing
 * content (Codex/Cursor-style).
 */
export const CHAT_CONTENT_MIN_WIDTH = 360;
export const CHAT_CONTENT_MAX_WIDTH = 760;
/** Preferred (max) chat input card width; matches the content column max. */
export const CHAT_INPUT_PREFERRED_WIDTH = CHAT_CONTENT_MAX_WIDTH;
/** Minimum chat input card width before extreme squeeze. */
export const CHAT_INPUT_MIN_WIDTH = 360;
/** Horizontal margin around the chat input within the middle column. */
export const CHAT_INPUT_HORIZONTAL_PADDING = 32;
export const LEFT_PANEL_MIN_WIDTH = 200;
export const LEFT_PANEL_MAX_WIDTH = 280;
export const LEFT_PANEL_DEFAULT_WIDTH = 260;
/** @deprecated Use {@link LEFT_PANEL_DEFAULT_WIDTH} — expanded width is user-resizable. */
export const LEFT_PANEL_WIDTH_EXPANDED = LEFT_PANEL_DEFAULT_WIDTH;
export const LEFT_PANEL_WIDTH_COLLAPSED = 56;

/** Width the middle column must reserve for the chat input at full size. */
export function getChatInputReservedWidth(): number {
  return CHAT_INPUT_PREFERRED_WIDTH + CHAT_INPUT_HORIZONTAL_PADDING;
}

/** Prevent CSS Grid from compressing a side column below its design width. */
export function fixedGridColumn(width: number): string {
  return `minmax(${width}px, ${width}px)`;
}

export function clampLeftPanelWidth(width: number): number {
  return Math.max(
    LEFT_PANEL_MIN_WIDTH,
    Math.min(width, LEFT_PANEL_MAX_WIDTH),
  );
}

export function getLeftPanelWidth(
  sidebarCollapsed: boolean,
  leftPanelWidth: number = LEFT_PANEL_DEFAULT_WIDTH,
): number {
  return sidebarCollapsed
    ? LEFT_PANEL_WIDTH_COLLAPSED
    : clampLeftPanelWidth(leftPanelWidth);
}

export function getRequiredWorkspaceWidth({
  sidebarCollapsed,
  rightPanelOpen,
  rightPanelWidth,
  leftPanelWidth = LEFT_PANEL_DEFAULT_WIDTH,
}: {
  sidebarCollapsed: boolean;
  rightPanelOpen: boolean;
  rightPanelWidth: number;
  leftPanelWidth?: number;
}): number {
  const left = getLeftPanelWidth(sidebarCollapsed, leftPanelWidth);
  const right = rightPanelOpen ? rightPanelWidth : 0;
  return left + right + getChatInputReservedWidth();
}

export function getMinimumWorkspaceWidth({
  sidebarCollapsed,
  rightPanelOpen,
  rightPanelWidth,
  leftPanelWidth = LEFT_PANEL_DEFAULT_WIDTH,
}: {
  sidebarCollapsed: boolean;
  rightPanelOpen: boolean;
  rightPanelWidth: number;
  leftPanelWidth?: number;
}): number {
  const left = getLeftPanelWidth(sidebarCollapsed, leftPanelWidth);
  const right = rightPanelOpen ? rightPanelWidth : 0;
  return left + right + CHAT_MIN_WIDTH;
}

export function getWorkspaceGridTemplateColumns({
  isConfigPanelOpen,
  isRightPanelOpen,
  sidebarCollapsed,
  rightPanelWidth = RIGHT_PANEL_DEFAULT_WIDTH,
  leftPanelWidth = LEFT_PANEL_DEFAULT_WIDTH,
}: {
  isConfigPanelOpen: boolean;
  isRightPanelOpen: boolean;
  sidebarCollapsed: boolean;
  rightPanelWidth?: number;
  leftPanelWidth?: number;
}): string {
  const leftColumn = fixedGridColumn(
    getLeftPanelWidth(sidebarCollapsed, leftPanelWidth),
  );
  const chatColumn = `minmax(${CHAT_MIN_WIDTH}px, 1fr)`;

  // Keep the track *count* constant (always three columns) so a CSS transition
  // on `grid-template-columns` can interpolate width smoothly. Removing/adding
  // the right track instead makes the transition discrete: the browser holds
  // the old track count for part of the duration, briefly reserving an empty
  // fixed-width column flush to the page's right edge while the panel DOM node
  // has already unmounted. Collapsing the right track to 0 avoids that gap.
  const rightVisible = !isConfigPanelOpen && isRightPanelOpen;
  const rightColumn = fixedGridColumn(rightVisible ? rightPanelWidth : 0);

  return `${leftColumn} ${chatColumn} ${rightColumn}`;
}

/**
 * Floor the right panel width to its absolute minimum. There is intentionally no
 * fixed upper cap here: the maximum is derived per-viewport by
 * {@link maxDockableRightPanelWidth} so that the chat column retains a constant
 * absolute minimum width regardless of screen size. Viewport pressure is handled
 * by `resolveResponsiveSidebars` (fold/close), not by shrinking panel widths.
 */
export function clampRightPanelWidth(width: number): number {
  return Math.max(RIGHT_PANEL_MIN_WIDTH, width);
}

/**
 * Keep user sidebar intent unless the layout cannot satisfy the chat minimum.
 * If the right panel causes minimum-width overflow, close it before folding left.
 */
export function resolveResponsiveSidebars({
  viewportWidth,
  userSidebarCollapsed,
  userRightPanelOpen,
  rightPanelWidth,
  leftPanelWidth = LEFT_PANEL_DEFAULT_WIDTH,
}: {
  viewportWidth: number;
  userSidebarCollapsed: boolean;
  userRightPanelOpen: boolean;
  rightPanelWidth: number;
  leftPanelWidth?: number;
}): {
  sidebarCollapsed: boolean;
  rightPanelOpen: boolean;
} {
  let sidebarCollapsed = userSidebarCollapsed;
  let rightPanelOpen = userRightPanelOpen;

  const minimumOverflows = () =>
    viewportWidth > 0 &&
    getMinimumWorkspaceWidth({
      sidebarCollapsed,
      rightPanelOpen,
      rightPanelWidth,
      leftPanelWidth,
    }) > viewportWidth;

  if (minimumOverflows() && rightPanelOpen) {
    rightPanelOpen = false;
  }

  if (minimumOverflows() && !sidebarCollapsed) {
    sidebarCollapsed = true;
  }

  return { sidebarCollapsed, rightPanelOpen };
}

/**
 * Expanding the left panel is an explicit user action and must never close the
 * right console. The centered chat column absorbs the width change (it can
 * shrink toward {@link CHAT_CONTENT_MIN_WIDTH}); genuinely tiny viewports are
 * still handled by {@link resolveResponsiveSidebars}.
 */
export function resolveSidebarExpandPreferences({
  userRightPanelOpen,
}: {
  viewportWidth: number;
  userRightPanelOpen: boolean;
  rightPanelWidth: number;
  leftPanelWidth?: number;
}): {
  userSidebarCollapsed: false;
  userRightPanelOpen: boolean;
} {
  return {
    userSidebarCollapsed: false,
    userRightPanelOpen,
  };
}

/**
 * Whether the viewport can fit a docked right panel (left collapsed + chat
 * *minimum* + right panel). When false, TaskConsole should use drawer mode.
 *
 * Uses the chat column minimum (not the preferred reservation): the chat can be
 * squeezed toward {@link CHAT_MIN_WIDTH} to keep the panel docked, so docking
 * should only fail when even the minimum layout overflows.
 */
export function canDockRightPanel({
  viewportWidth,
  rightPanelWidth,
}: {
  viewportWidth: number;
  rightPanelWidth: number;
}): boolean {
  if (viewportWidth <= 0) return true;
  return (
    getMinimumWorkspaceWidth({
      sidebarCollapsed: true,
      rightPanelOpen: true,
      rightPanelWidth,
    }) <= viewportWidth
  );
}

/**
 * Largest right-panel width that still keeps the panel docked at the given
 * viewport, alongside the current left sidebar with the chat squeezed to its
 * absolute minimum ({@link CHAT_MIN_WIDTH}). This is the single source of the
 * upper bound: it is derived purely from the viewport (no fixed cap), so the
 * chat column keeps the same absolute minimum width at every screen size and the
 * panel may grow wide on large viewports. Drag handling clamps to this so the
 * user can never widen the panel into the responsive "close right / undock"
 * path. Returns the fallback max when the viewport is unknown (SSR/hydration).
 */
export function maxDockableRightPanelWidth(
  viewportWidth: number,
  leftPanelWidth: number = LEFT_PANEL_WIDTH_COLLAPSED,
): number {
  if (viewportWidth <= 0) return RIGHT_PANEL_MAX_WIDTH;
  const available = viewportWidth - leftPanelWidth - CHAT_MIN_WIDTH;
  return Math.max(RIGHT_PANEL_MIN_WIDTH, available);
}

export const chatPaneClassName =
  "min-h-0 w-full flex-1 [&_[data-testid='copilot-scrollable']]:pb-32 " +
  // Center every message row within the content column max-width so the chat
  // reads as a centered column that shifts (not reflows) as side panels toggle.
  "[&_.copilotKitMessage]:mx-auto [&_.copilotKitMessage]:w-full [&_.copilotKitMessage]:max-w-[760px]";
