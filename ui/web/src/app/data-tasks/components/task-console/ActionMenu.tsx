"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { IconChevronDown } from "./console-icons";

export type ActionMenuItem = {
  key: string;
  label: ReactNode;
  onSelect: () => void;
  disabled?: boolean;
};

/**
 * Lightweight dropdown used for the outputs card `Download` and overflow (`…`)
 * menus. Closes on outside click or Escape. Renders nothing when `items` is empty.
 */
export function ActionMenu({
  items,
  triggerClass,
  triggerIcon,
  triggerLabel,
  ariaLabel,
  title,
  showChevron = true,
  align = "left",
  placement = "down",
}: {
  items: ActionMenuItem[];
  triggerClass: string;
  triggerIcon?: ReactNode;
  triggerLabel?: ReactNode;
  ariaLabel?: string;
  title?: string;
  showChevron?: boolean;
  align?: "left" | "right";
  placement?: "up" | "down";
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocMouseDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDocMouseDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (items.length === 0) return null;

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => setOpen((value) => !value)}
        className={triggerClass}
      >
        {triggerIcon}
        {triggerLabel}
        {showChevron ? <IconChevronDown className="h-3.5 w-3.5" /> : null}
      </button>
      {open ? (
        <div
          role="menu"
          className={[
            "absolute z-40 min-w-[168px] overflow-hidden rounded-lg border border-border bg-surface py-1 shadow-lg",
            placement === "up" ? "bottom-full mb-1" : "top-full mt-1",
            align === "right" ? "right-0" : "left-0",
          ].join(" ")}
        >
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              onClick={() => {
                if (item.disabled) return;
                setOpen(false);
                item.onSelect();
              }}
              className="flex w-full cursor-pointer items-center gap-2 px-3 py-1.5 text-left text-xs text-muted transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
