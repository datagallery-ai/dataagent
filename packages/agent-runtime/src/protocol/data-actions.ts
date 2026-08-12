/**
 * Single source of truth for the data-facing agent action names that formal
 * protocols govern. general-task excludes every entry; data-analysis opens
 * them per phase. Keep instructions, protocols, and completion checks on this
 * constant instead of re-declaring the list.
 */
export const DATA_ACTION_NAMES = [
  "list_data_sources",
  "inspect_schema",
  "preview_table",
  "run_sql_readonly"
] as const;

export const DATA_ACTIONS: ReadonlySet<string> = new Set(DATA_ACTION_NAMES);

export const isDataActionName = (name: string): boolean => DATA_ACTIONS.has(name);
