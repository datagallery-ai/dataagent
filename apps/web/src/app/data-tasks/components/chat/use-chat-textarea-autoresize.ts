"use client";

import { useCallback, useEffect, useRef } from "react";

/** Match CopilotKit default: grow with content, scroll after ~6 lines. */
const MAX_TEXTAREA_ROWS = 6;

export function resizeChatTextarea(textarea: HTMLTextAreaElement): void {
  const computed = window.getComputedStyle(textarea);
  const lineHeight = parseFloat(computed.lineHeight) || 24;
  const paddingTop = parseFloat(computed.paddingTop) || 0;
  const paddingBottom = parseFloat(computed.paddingBottom) || 0;
  const maxHeight = lineHeight * MAX_TEXTAREA_ROWS + paddingTop + paddingBottom;

  textarea.style.maxHeight = `${maxHeight}px`;
  textarea.style.overflowY = "hidden";
  textarea.style.height = "auto";

  const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
  textarea.style.height = `${nextHeight}px`;
  textarea.style.overflowY =
    textarea.scrollHeight > maxHeight ? "auto" : "hidden";
}

let activeScheduleResize: (() => void) | null = null;

export function scheduleChatTextareaResize(): void {
  activeScheduleResize?.();
}

/**
 * CopilotKit's built-in auto-grow only runs in its default grid layout. Our
 * custom input shell must resize the textarea itself.
 */
export function useChatTextareaAutoresize(refreshToken?: string) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

  const scheduleResize = useCallback(() => {
    if (textareaRef.current) {
      resizeChatTextarea(textareaRef.current);
    }
  }, []);

  const bindContainer = useCallback(
    (node: HTMLDivElement | null) => {
      cleanupRef.current?.();
      cleanupRef.current = null;
      containerRef.current = node;

      const textarea = node?.querySelector("textarea") ?? null;
      textareaRef.current = textarea;
      if (!textarea) return;

      textarea.style.resize = "none";

      scheduleResize();
      textarea.addEventListener("input", scheduleResize);

      const observer =
        typeof ResizeObserver !== "undefined"
          ? new ResizeObserver(scheduleResize)
          : null;
      observer?.observe(textarea);

      cleanupRef.current = () => {
        textarea.removeEventListener("input", scheduleResize);
        observer?.disconnect();
        if (textareaRef.current === textarea) {
          textareaRef.current = null;
        }
      };
    },
    [scheduleResize],
  );

  useEffect(() => {
    activeScheduleResize = scheduleResize;
    return () => {
      if (activeScheduleResize === scheduleResize) {
        activeScheduleResize = null;
      }
    };
  }, [scheduleResize]);

  useEffect(() => {
    bindContainer(containerRef.current);
  }, [bindContainer, refreshToken]);

  useEffect(() => () => cleanupRef.current?.(), []);

  return bindContainer;
}
