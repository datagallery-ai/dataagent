"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import {
  loadLeftPanelWidth,
  persistLeftPanelWidth,
} from "../data-task-state";
import {
  clampLeftPanelWidth,
  LEFT_PANEL_DEFAULT_WIDTH,
} from "../workspace-layout";

type UseLeftPanelResizeOptions = {
  enabled: boolean;
};

type UseLeftPanelResizeResult = {
  width: number;
  isResizing: boolean;
  onResizeStart: (event: ReactPointerEvent<HTMLElement>) => void;
  resetWidth: () => void;
};

/**
 * Owns the left sidebar width: drag-to-resize from the panel's right edge,
 * absolute min/max clamping, persistence to localStorage, and a double-click
 * reset to the default width.
 */
export function useLeftPanelResize({
  enabled,
}: UseLeftPanelResizeOptions): UseLeftPanelResizeResult {
  const [width, setWidth] = useState<number>(() => loadLeftPanelWidth());
  const [isResizing, setIsResizing] = useState(false);

  const dragStateRef = useRef<{ startX: number; startWidth: number } | null>(
    null,
  );

  const applyWidth = useCallback((next: number) => {
    setWidth(clampLeftPanelWidth(next));
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
    applyWidth(LEFT_PANEL_DEFAULT_WIDTH);
    persistLeftPanelWidth(LEFT_PANEL_DEFAULT_WIDTH);
  }, [applyWidth]);

  useEffect(() => {
    if (!isResizing) return;

    const handlePointerMove = (event: PointerEvent) => {
      const drag = dragStateRef.current;
      if (!drag) return;
      const next = drag.startWidth + (event.clientX - drag.startX);
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

  useEffect(() => {
    if (!isResizing || !enabled) return;
    persistLeftPanelWidth(width);
  }, [isResizing, enabled, width]);

  return { width, isResizing, onResizeStart, resetWidth };
}
