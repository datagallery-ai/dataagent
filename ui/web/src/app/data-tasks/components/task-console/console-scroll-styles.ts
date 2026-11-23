/** Shared overflow helpers for the resizable right console at narrow widths. */
import { codeBlockClass, dataTableShellClass } from "../../ui-tokens";

export const consoleScrollXShellClass =
  "max-w-full overflow-x-auto overscroll-x-contain";

export const consoleCodeBlockBaseClass = codeBlockClass;

export const consoleCodeInnerClass = "block min-w-max whitespace-pre font-mono";

export const consoleTableShellClass = dataTableShellClass;

/** Vertical step list in overview — cap height so sibling sections stay visible. */
export const consoleStepsListClass =
  "max-h-80 overflow-y-auto overscroll-y-contain pr-0.5";
