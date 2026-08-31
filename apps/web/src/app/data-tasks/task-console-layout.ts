export type ConsoleTabId = "overview" | "trace" | "outputs" | "detail";

export type OverviewSectionId = "conclusion" | "progress" | "tool-distribution";

export type OverviewSectionPlanItem = {
  id: OverviewSectionId;
  collapsible: boolean;
};

export const CONSOLE_FILE_ASSETS_TAB: ConsoleTabId = "outputs";

/** Fixed peer-page id for the Task Console tab next to opened artifact pages. */
export const CONSOLE_PEER_PAGE_ID = "console";

/**
 * When the user picks a tool chip / process step in chat, Details must be visible —
 * jump back from any artifact peer page onto the console page.
 */
export function shouldRevealConsoleForSelection(
  selection: { type: string } | null | undefined,
): boolean {
  return selection?.type === "action" || selection?.type === "toolGroup";
}

/**
 * Escape handling priority inside the task console shell.
 * Higher layers consume Escape (capture + stopImmediatePropagation) so lower
 * layers — especially the mobile console drawer — do not also dismiss.
 */
export type ConsoleEscapeLayer = "preview" | "peerPage" | "drawer";

export function nextConsoleEscapeAction(input: {
  previewOpen: boolean;
  onPeerPage: boolean;
  drawerOpen: boolean;
}): ConsoleEscapeLayer | null {
  if (input.previewOpen) return "preview";
  if (input.onPeerPage) return "peerPage";
  if (input.drawerOpen) return "drawer";
  return null;
}

export function overviewSectionPlan({
  hasToolDistribution,
}: {
  hasToolDistribution: boolean;
}): OverviewSectionPlanItem[] {
  return [
    { id: "conclusion", collapsible: false },
    { id: "progress", collapsible: false },
    ...(hasToolDistribution ? [{ id: "tool-distribution", collapsible: true } as const] : []),
  ];
}
