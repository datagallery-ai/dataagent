import type { ConfigFieldDef, WorkspaceConfigKind } from "../app/data-tasks/data-task-state";
import type { TranslateFn } from "./types";

/** Resolve a message key, falling back to the provided English/default copy. */
export function tx(t: TranslateFn, key: string, fallback: string): string {
  const value = t(key);
  return value === key ? fallback : value;
}

export function translateConfigField(
  kind: WorkspaceConfigKind,
  field: ConfigFieldDef,
  t: TranslateFn,
): Pick<ConfigFieldDef, "label" | "placeholder" | "helpText"> {
  const base = `configFields.${kind}.${field.key}`;
  return {
    label: tx(t, `${base}.label`, field.label),
    placeholder: field.placeholder
      ? tx(t, `${base}.placeholder`, field.placeholder)
      : field.placeholder,
    helpText: field.helpText
      ? tx(t, `${base}.helpText`, field.helpText)
      : field.helpText,
  };
}

export function translateConfigOptionLabel(
  kind: WorkspaceConfigKind,
  fieldKey: string,
  value: string,
  fallback: string,
  t: TranslateFn,
): string {
  const optionValue = value.trim() === "" ? "__none__" : value;
  return tx(t, `configOptions.${kind}.${fieldKey}.${optionValue}`, fallback);
}

export function translateConfigFieldOptions(
  kind: WorkspaceConfigKind,
  field: ConfigFieldDef,
  options: Array<{ value: string; label: string }>,
  t: TranslateFn,
): Array<{ value: string; label: string }> {
  return options.map((option) => ({
    ...option,
    label: translateConfigOptionLabel(kind, field.key, option.value, option.label, t),
  }));
}
