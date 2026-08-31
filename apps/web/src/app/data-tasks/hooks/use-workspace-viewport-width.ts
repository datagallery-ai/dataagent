"use client";

import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

type UseWorkspaceViewportWidthResult = {
  containerRef: (node: HTMLDivElement | null) => void;
  viewportWidth: number;
  isViewportResizing: boolean;
};

function readContainerWidth(element: HTMLElement): number {
  const width = element.clientWidth;
  return width > 0 ? width : 0;
}

/**
 * Tracks the live width of the workspace grid container so responsive fold
 * rules use the same box the columns are laid out in (not window.innerWidth).
 */
export function useWorkspaceViewportWidth(
  enabled: boolean,
): UseWorkspaceViewportWidthResult {
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const [viewportWidth, setViewportWidth] = useState(1280);
  const [isViewportResizing, setIsViewportResizing] = useState(false);
  const resizeEndTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const containerRef = useCallback((node: HTMLDivElement | null) => {
    setContainer(node);
    if (!node) return;

    const width = readContainerWidth(node);
    if (width > 0) {
      setViewportWidth(width);
    }
  }, []);

  useLayoutEffect(() => {
    if (!enabled || !container) return;

    const markResizing = () => {
      setIsViewportResizing(true);
      if (resizeEndTimerRef.current) {
        clearTimeout(resizeEndTimerRef.current);
      }
      resizeEndTimerRef.current = setTimeout(() => {
        setIsViewportResizing(false);
      }, 150);
    };

    const update = () => {
      const width = readContainerWidth(container);
      if (width > 0) {
        setViewportWidth(width);
        markResizing();
      }
    };

    update();

    const observer = new ResizeObserver(update);
    observer.observe(container);

    return () => {
      observer.disconnect();
      if (resizeEndTimerRef.current) {
        clearTimeout(resizeEndTimerRef.current);
      }
    };
  }, [container, enabled]);

  return { containerRef, viewportWidth, isViewportResizing };
}
