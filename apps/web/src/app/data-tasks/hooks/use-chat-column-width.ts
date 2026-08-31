"use client";

import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
} from "react";

type UseChatColumnWidthResult = {
  containerRef: (node: HTMLDivElement | null) => void;
  chatColumnWidth: number;
};

function readContainerWidth(element: HTMLElement): number {
  const width = element.clientWidth;
  return width > 0 ? width : 0;
}

/**
 * Tracks the live width of the chat column content area for input sizing.
 */
export function useChatColumnWidth(): UseChatColumnWidthResult {
  const [container, setContainer] = useState<HTMLDivElement | null>(null);
  const [chatColumnWidth, setChatColumnWidth] = useState(() =>
    typeof window !== "undefined" ? window.innerWidth : 1280,
  );
  const observerRef = useRef<ResizeObserver | null>(null);

  const containerRef = useCallback((node: HTMLDivElement | null) => {
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }

    setContainer(node);
    if (!node) return;

    const width = readContainerWidth(node);
    if (width > 0) {
      setChatColumnWidth(width);
    }
  }, []);

  useLayoutEffect(() => {
    if (!container) return;

    const update = () => {
      const width = readContainerWidth(container);
      if (width > 0) {
        setChatColumnWidth(width);
      }
    };

    update();

    const observer = new ResizeObserver(update);
    observer.observe(container);
    observerRef.current = observer;

    return () => {
      observer.disconnect();
      observerRef.current = null;
    };
  }, [container]);

  return { containerRef, chatColumnWidth };
}
