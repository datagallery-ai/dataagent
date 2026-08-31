"use client";

import type { PointerEvent as ReactPointerEvent } from "react";

type PanelResizeHandleProps = {
  edge: "left" | "right";
  width: number;
  minWidth: number;
  maxWidth: number;
  label: string;
  isResizing: boolean;
  onResizeStart: (event: ReactPointerEvent<HTMLElement>) => void;
  onReset: () => void;
};

/**
 * Vertical drag handle overlaying a panel edge without occupying its own column.
 * `edge="left"` sits on the panel's left edge (right console); `edge="right"` on
 * the right edge (left sidebar).
 */
export function PanelResizeHandle({
  edge,
  width,
  minWidth,
  maxWidth,
  label,
  isResizing,
  onResizeStart,
  onReset,
}: PanelResizeHandleProps) {
  const edgeClass =
    edge === "left"
      ? "left-0 -translate-x-1/2"
      : "right-0 translate-x-1/2";

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={Math.round(width)}
      aria-valuemin={minWidth}
      aria-valuemax={maxWidth}
      tabIndex={0}
      onPointerDown={onResizeStart}
      onDoubleClick={onReset}
      title="Drag to resize, double-click to reset"
      className={[
        "absolute top-0 z-10 h-full w-1.5 cursor-col-resize",
        edgeClass,
        "transition-colors duration-150",
        "before:absolute before:inset-y-0 before:left-1/2 before:w-px before:-translate-x-1/2 before:bg-border before:content-['']",
        "hover:before:bg-primary-light",
        isResizing ? "before:bg-primary" : "",
      ].join(" ")}
    />
  );
}
