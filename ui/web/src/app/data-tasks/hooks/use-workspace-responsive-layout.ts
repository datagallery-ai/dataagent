"use client";

import { useMemo } from "react";
import {
  canDockRightPanel as computeCanDockRightPanel,
  resolveResponsiveSidebars,
} from "../workspace-layout";

type UseWorkspaceResponsiveLayoutOptions = {
  viewportWidth: number;
  userSidebarCollapsed: boolean;
  userRightPanelOpen: boolean;
  rightPanelWidth: number;
  leftPanelWidth: number;
  enabled: boolean;
};

type UseWorkspaceResponsiveLayoutResult = {
  sidebarCollapsed: boolean;
  rightPanelOpen: boolean;
  isAutoLayout: boolean;
  /** False when viewport is too narrow for a docked right panel; use drawer instead. */
  canDockRightPanel: boolean;
};

/**
 * Applies viewport-driven sidebar overrides: close right first, then collapse
 * left, while preserving user preferences for restoration when space returns.
 */
export function useWorkspaceResponsiveLayout({
  viewportWidth,
  userSidebarCollapsed,
  userRightPanelOpen,
  rightPanelWidth,
  leftPanelWidth,
  enabled,
}: UseWorkspaceResponsiveLayoutOptions): UseWorkspaceResponsiveLayoutResult {
  return useMemo(() => {
    if (!enabled) {
      return {
        sidebarCollapsed: userSidebarCollapsed,
        rightPanelOpen: userRightPanelOpen,
        isAutoLayout: false,
        canDockRightPanel: true,
      };
    }

    const dockable = computeCanDockRightPanel({
      viewportWidth,
      rightPanelWidth,
    });

    const resolved = resolveResponsiveSidebars({
      viewportWidth,
      userSidebarCollapsed,
      userRightPanelOpen: dockable ? userRightPanelOpen : false,
      rightPanelWidth,
      leftPanelWidth,
    });

    return {
      ...resolved,
      isAutoLayout:
        resolved.sidebarCollapsed !== userSidebarCollapsed ||
        (dockable && resolved.rightPanelOpen !== userRightPanelOpen),
      canDockRightPanel: dockable,
    };
  }, [
    enabled,
    viewportWidth,
    userSidebarCollapsed,
    userRightPanelOpen,
    rightPanelWidth,
    leftPanelWidth,
  ]);
}
