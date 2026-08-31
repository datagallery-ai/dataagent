"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  loadRightPanelWidth,
  persistRightPanelWidth,
} from "../data-task-state";
import {
  clampRightPanelWidth,
  RIGHT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_MAX_WIDTH,
} from "../workspace-layout";

type UsePanelResizeOptions = {
  enabled: boolean;
  /**
   * Dynamic upper bound (from viewport) that keeps the panel dockable. The
   * stored width still remembers the user's larger intent; only the returned
   * effective width is capped, so it restores when the viewport grows again.
   */
  maxWidth?: number;
};

type UsePanelResizeResult = {
  width: number;
  isResizing: boolean;
  onResizeStart: (event: ReactPointerEvent<HTMLElement>) => void;
  resetWidth: () => void;
};

/**
 * Owns the right console width: drag-to-resize from the panel's left edge,
 * absolute min/max clamping (320–640px), persistence to localStorage, and a
 * double-click reset to the default width. Window resize does not shrink the
 * stored width — responsive fold/close is handled separately.
 */
export function usePanelResize({
  enabled,
  maxWidth = RIGHT_PANEL_MAX_WIDTH,
}: UsePanelResizeOptions): UsePanelResizeResult {
  // `storedWidth` remembers the user's intended width (persisted); the returned
  // `width` is additionally capped to the dockable max so the panel can never
  // be widened into an undockable state.
  const [storedWidth, setStoredWidth] = useState<number>(() =>
    loadRightPanelWidth(),
  );
  const [isResizing, setIsResizing] = useState(false);

  const effectiveMax = clampRightPanelWidth(maxWidth);
  const width = Math.min(storedWidth, effectiveMax);

  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(
    null,
  );

  const applyWidth = useCallback((next: number) => {
    setStoredWidth(clampRightPanelWidth(next));
  }, []);

  const onResizeStart = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.setPointerCapture(event.pointerId);
      dragStateRef.current = { startX: event.clientX, startWidth: width };
      setIsResizing(true);
    },
    [width],
  );

  const resetWidth = useCallback(() => {
    applyWidth(RIGHT_PANEL_DEFAULT_WIDTH);
    persistRightPanelWidth(RIGHT_PANEL_DEFAULT_WIDTH);
  }, [applyWidth]);

  // Global pointer handlers active only while dragging.
  useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag) return;
      // Drag from the panel's left edge: moving left widens the panel.
      const next = drag.startWidth - (event.clientX - drag.startX);
      applyWidth(next);
    };

    const stop = () => {
      dragStateRef.current = null;
      setIsResizing(false);
    };

    document.addEventListener("pointermove", handlePointerMove);
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);

    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    return () => {
      document.removeEventListener("pointermove", handlePointerMove);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
    };
  }, [isResizing, applyWidth]);

  // Persist the user's intended width (not the viewport-capped value) so it
  // restores when the viewport grows back.
  useEffect(() => {
    if (!isResizing || !enabled) return;
    persistRightPanelWidth(storedWidth);
  }, [isResizing, enabled, storedWidth]);

  return { width, isResizing, onResizeStart, resetWidth };
}
