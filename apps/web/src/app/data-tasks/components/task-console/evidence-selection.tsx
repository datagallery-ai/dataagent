"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { EvidenceSelection } from "@datafoundry/contracts";
import { useT } from "../../../../i18n/locale-context";
import { describeEvidenceSelection } from "../../evidence";
import { consoleTableShellClass } from "./console-scroll-styles";

type Point = { x: number; y: number };

/**
 * Small floating action rendered at a viewport coordinate. Used to turn an active
 * selection (table region or highlighted text) into an evidence reference.
 */
function FloatingReferenceButton({
  anchor,
  title,
  onReference,
}: {
  anchor: Point;
  title: string;
  onReference: () => void;
}) {
  const t = useT();
  return (
    <div
      className="fixed z-[70] -translate-x-1/2 -translate-y-full"
      style={{ left: anchor.x, top: Math.max(anchor.y - 8, 12) }}
      onMouseDown={(event) => event.preventDefault()}
    >
      <button
        type="button"
        title={title}
        onClick={onReference}
        className="cursor-pointer rounded-lg bg-primary px-2.5 py-1 text-[11px] font-semibold text-white shadow-lg transition-colors hover:bg-primary/90"
      >
        {t("console.reference")}
      </button>
    </div>
  );
}

function useDismissOnOutside(
  active: boolean,
  clear: () => void,
  containerRef: { current: HTMLElement | null },
) {
  useEffect(() => {
    if (!active) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (containerRef.current?.contains(target)) return;
      clear();
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [active, clear, containerRef]);
}

type GridDragMode = "cells" | "rows" | "cols";

type GridSelection = {
  mode: GridDragMode;
  r0: number;
  c0: number;
  r1: number;
  c1: number;
};

function normalizeRange(sel: GridSelection): { r0: number; c0: number; r1: number; c1: number } {
  return {
    r0: Math.min(sel.r0, sel.r1),
    r1: Math.max(sel.r0, sel.r1),
    c0: Math.min(sel.c0, sel.c1),
    c1: Math.max(sel.c0, sel.c1),
  };
}

function cellInSelection(sel: GridSelection | null, r: number, c: number): boolean {
  if (!sel) return false;
  const range = normalizeRange(sel);
  return r >= range.r0 && r <= range.r1 && c >= range.c0 && c <= range.c1;
}

/**
 * A read-only data grid that lets the user drag-select a rectangular cell range,
 * click a header to select a whole column, or click a row number to select a whole
 * row. The active selection surfaces a floating "Reference" action.
 */
export function SelectableDataGrid({
  columns,
  rows,
  onReference,
}: {
  columns: string[];
  rows: string[][];
  onReference: (selection: EvidenceSelection) => void;
}) {
  const [selection, setSelection] = useState<GridSelection | null>(null);
  const [anchor, setAnchor] = useState<Point | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const dragMode = useRef<GridDragMode | null>(null);
  const colCount = columns.length;
  const rowCount = rows.length;

  const finishDrag = useCallback((event: MouseEvent) => {
    if (!dragMode.current) return;
    dragMode.current = null;
    setAnchor({ x: event.clientX, y: event.clientY });
  }, []);

  useEffect(() => {
    window.addEventListener("mouseup", finishDrag);
    return () => window.removeEventListener("mouseup", finishDrag);
  }, [finishDrag]);

  const clear = useCallback(() => {
    setSelection(null);
    setAnchor(null);
    dragMode.current = null;
  }, []);

  const startCell = (r: number, c: number) => {
    dragMode.current = "cells";
    setAnchor(null);
    setSelection({ mode: "cells", r0: r, c0: c, r1: r, c1: c });
  };
  const startCol = (c: number) => {
    dragMode.current = "cols";
    setAnchor(null);
    setSelection({ mode: "cols", r0: 0, c0: c, r1: Math.max(rowCount - 1, 0), c1: c });
  };
  const startRow = (r: number) => {
    dragMode.current = "rows";
    setAnchor(null);
    setSelection({ mode: "rows", r0: r, c0: 0, r1: r, c1: Math.max(colCount - 1, 0) });
  };
  const extendTo = (r: number, c: number) => {
    if (!dragMode.current) return;
    setSelection((current) => {
      if (!current) return current;
      if (current.mode === "cols") return { ...current, c1: c };
      if (current.mode === "rows") return { ...current, r1: r };
      return { ...current, r1: r, c1: c };
    });
  };

  const selectAll = () => {
    if (rowCount === 0 || colCount === 0) return;
    dragMode.current = null;
    setSelection({ mode: "cells", r0: 0, c0: 0, r1: rowCount - 1, c1: colCount - 1 });
    setAnchor({ x: window.innerWidth / 2, y: 120 });
  };

  const toEvidenceSelection = (sel: GridSelection): EvidenceSelection => {
    const range = normalizeRange(sel);
    if (sel.mode === "cols") {
      return {
        mode: "cols",
        range,
        columns: columns.slice(range.c0, range.c1 + 1),
      };
    }
    if (sel.mode === "rows") {
      return { mode: "rows", range };
    }
    return { mode: "cells", range };
  };

  const activeSelection = selection ? toEvidenceSelection(selection) : null;
  const showReference = Boolean(anchor && activeSelection);

  useDismissOnOutside(showReference, clear, rootRef);

  return (
    <div ref={rootRef} className="grid gap-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] text-muted-light">
          Drag cells, or click a column header / row number to select. {rowCount.toLocaleString()} rows.
        </span>
        <button
          type="button"
          onClick={selectAll}
          className="cursor-pointer rounded-md border border-border bg-surface px-2 py-1 text-[11px] font-medium text-muted transition-colors hover:text-foreground"
        >
          Select all
        </button>
      </div>
      <div className={consoleTableShellClass}>
        <div className="max-h-[min(560px,64vh)] overflow-auto">
          <table className="min-w-max w-full text-left text-xs select-none">
            <thead className="sticky top-0 z-10 bg-surface-subtle text-muted-light shadow-[0_1px_0_0_var(--border)]">
              <tr>
                <th className="sticky left-0 z-20 bg-surface-subtle px-2 py-2 text-right font-semibold text-muted-light">
                  #
                </th>
                {columns.map((column, columnIndex) => {
                  const active =
                    selection?.mode === "cols" &&
                    columnIndex >= Math.min(selection.c0, selection.c1) &&
                    columnIndex <= Math.max(selection.c0, selection.c1);
                  return (
                    <th
                      key={`${column}-${columnIndex}`}
                      onMouseDown={() => startCol(columnIndex)}
                      onMouseEnter={() => dragMode.current === "cols" && extendTo(0, columnIndex)}
                      className={[
                        "cursor-pointer whitespace-nowrap px-3 py-2 font-semibold transition-colors",
                        active ? "bg-primary-light/25 text-foreground" : "hover:text-foreground",
                      ].join(" ")}
                      title={`Select column ${column}`}
                    >
                      {column}
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {rowCount === 0 ? (
                <tr>
                  <td
                    colSpan={colCount + 1}
                    className="border-t border-border px-3 py-6 text-center text-muted-light"
                  >
                    No result rows.
                  </td>
                </tr>
              ) : (
                rows.map((row, rowIndex) => {
                  const rowActive =
                    selection?.mode === "rows" &&
                    rowIndex >= Math.min(selection.r0, selection.r1) &&
                    rowIndex <= Math.max(selection.r0, selection.r1);
                  return (
                    <tr key={rowIndex} className="border-t border-border">
                      <td
                        onMouseDown={() => startRow(rowIndex)}
                        onMouseEnter={() => dragMode.current === "rows" && extendTo(rowIndex, 0)}
                        className={[
                          "sticky left-0 z-10 cursor-pointer bg-surface px-2 py-2 text-right font-mono text-[10px] text-muted-light transition-colors",
                          rowActive ? "bg-primary-light/25 text-foreground" : "hover:text-foreground",
                        ].join(" ")}
                        title={`Select row ${rowIndex + 1}`}
                      >
                        {rowIndex + 1}
                      </td>
                      {row.map((cell, cellIndex) => {
                        const selected = cellInSelection(selection, rowIndex, cellIndex);
                        return (
                          <td
                            key={cellIndex}
                            onMouseDown={() => startCell(rowIndex, cellIndex)}
                            onMouseEnter={() => extendTo(rowIndex, cellIndex)}
                            className={[
                              "cursor-cell whitespace-nowrap px-3 py-2 transition-colors",
                              selected
                                ? "bg-primary-light/25 text-foreground"
                                : cellIndex === 0
                                  ? "font-medium text-foreground"
                                  : "text-muted",
                            ].join(" ")}
                          >
                            {formatCell(cell)}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>
      {showReference && activeSelection ? (
        <FloatingReferenceButton
          anchor={anchor!}
          title={describeEvidenceSelection(activeSelection)}
          onReference={() => {
            onReference(activeSelection);
            clear();
          }}
        />
      ) : null}
    </div>
  );
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/**
 * Wraps arbitrary rendered content (markdown, report text, file previews) and
 * surfaces a floating "Reference" action whenever the user highlights text inside it.
 */
export function SelectableText({
  onReference,
  children,
}: {
  onReference: (selection: EvidenceSelection) => void;
  children: ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [anchor, setAnchor] = useState<Point | null>(null);
  const [quote, setQuote] = useState("");

  const clear = useCallback(() => {
    setAnchor(null);
    setQuote("");
    window.getSelection()?.removeAllRanges();
  }, []);

  const captureSelection = useCallback(() => {
    const domSelection = window.getSelection();
    if (!domSelection || domSelection.isCollapsed || domSelection.rangeCount === 0) {
      clear();
      return;
    }
    const range = domSelection.getRangeAt(0);
    const container = containerRef.current;
    if (!container || !container.contains(range.commonAncestorContainer)) {
      return;
    }
    const text = domSelection.toString().trim();
    if (!text) {
      clear();
      return;
    }
    const rect = range.getBoundingClientRect();
    setQuote(text);
    setAnchor({ x: rect.left + rect.width / 2, y: rect.top });
  }, [clear]);

  useDismissOnOutside(Boolean(anchor && quote), clear, containerRef);

  return (
    <div ref={containerRef} onMouseUp={captureSelection} className="min-w-0 w-full max-w-full">
      {children}
      {anchor && quote ? (
        <FloatingReferenceButton
          anchor={anchor}
          title={quote.length > 120 ? `${quote.slice(0, 120)}…` : quote}
          onReference={() => {
            onReference({ mode: "text", quote });
            clear();
          }}
        />
      ) : null}
    </div>
  );
}
