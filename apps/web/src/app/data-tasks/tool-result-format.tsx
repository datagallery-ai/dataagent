"use client";

import React, { type ReactNode } from "react";

import { resolveToolFailurePresentation } from "./tool-call-display";
import { normalizeTableRows, normalizeSqlTable } from "./table-rows";
import {
  parseSchemaToolResult,
  parseSqlToolResult,
  parseToolResultRecord,
  toolResultObservationText,
  unwrapToolResultPayload,
} from "./tool-result-normalize";
import {
  codeBlockClass,
  dataTableCellClass,
  dataTableCellNumericClass,
  dataTableClass,
  dataTableHeadClass,
  dataTableRowClass,
  dataTableShellClass,
  statusTone,
} from "./ui-tokens";

function parseParamsRecord(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : null;
    } catch {
      return null;
    }
  }
  return null;
}

/** Field names whose string values render as monospace code blocks. */
const CODE_PARAM_KEYS = new Set([
  "sql",
  "command",
  "content",
  "old_string",
  "new_string",
  "query",
  "pattern",
]);

const LONG_STRING_CHARS = 80;

function formatParamPrimitive(value: unknown): string {
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") return String(value);
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function shouldSkipParam(_key: string, value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

function ParamCodeBlock({ name, value }: { name: string; value: string }) {
  const preview = value.length > 4000 ? `${value.slice(0, 4000)}…` : value;
  return (
    <div className="grid gap-1">
      <div className="font-mono text-[10px] font-semibold text-muted-light">{name}</div>
      <pre className={`max-h-60 ${codeBlockClass}`}>
        <code>{preview}</code>
      </pre>
    </div>
  );
}

function ParamScalar({ name, value }: { name: string; value: unknown }) {
  const text = formatParamPrimitive(value);
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded-lg border border-border bg-surface/80 px-2 py-1.5 text-xs">
      <span className="font-mono text-[10px] font-semibold text-muted-light">{name}</span>
      <span className="font-mono text-foreground">{text}</span>
    </div>
  );
}

function ParamArray({ name, value }: { name: string; value: unknown[] }) {
  const items = value.map((item) => formatParamPrimitive(item));
  if (items.length === 0) return null;
  return (
    <div className="grid gap-1">
      <div className="font-mono text-[10px] font-semibold text-muted-light">{name}</div>
      <div className="flex flex-wrap gap-1">
        {items.map((item, index) => (
          <span
            key={`${name}-${index}`}
            className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted"
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}

function ParamObject({ name, value }: { name: string; value: Record<string, unknown> }) {
  return (
    <div className="grid gap-1">
      <div className="font-mono text-[10px] font-semibold text-muted-light">{name}</div>
      <pre className="max-h-40 overflow-auto rounded-lg bg-surface-subtle p-2 text-[11px] leading-5 text-muted">
        {formatPayload(value)}
      </pre>
    </div>
  );
}

function renderParamEntry(name: string, value: unknown): ReactNode {
  if (typeof value === "string") {
    const useCodeBlock =
      CODE_PARAM_KEYS.has(name) ||
      value.includes("\n") ||
      value.length > LONG_STRING_CHARS;
    if (useCodeBlock) {
      return <ParamCodeBlock key={name} name={name} value={value} />;
    }
  }

  if (Array.isArray(value)) {
    return <ParamArray key={name} name={name} value={value} />;
  }

  if (value && typeof value === "object") {
    return <ParamObject key={name} name={name} value={value as Record<string, unknown>} />;
  }

  return <ParamScalar key={name} name={name} value={value} />;
}

/** Tool-agnostic parameter renderer — works for any current or future tool. */
export function renderFormattedToolParams(parameters: unknown): ReactNode | null {
  const record = parseParamsRecord(parameters);
  if (!record) {
    if (parameters === undefined) return null;
    return (
      <pre className={`max-h-40 ${codeBlockClass}`}>
        {formatPayload(parameters)}
      </pre>
    );
  }

  const entries = Object.entries(record).filter(([key, value]) => !shouldSkipParam(key, value));
  if (entries.length === 0) return null;

  return (
    <div className="grid gap-2">
      {entries.map(([key, value]) => renderParamEntry(key, value))}
    </div>
  );
}

export function ToolFormattedParams({
  parameters,
}: {
  toolName?: string;
  parameters: unknown;
}) {
  if (parameters === undefined) return null;

  const formatted = renderFormattedToolParams(parameters);
  if (!formatted) {
    return (
      <div className="mt-2 rounded-lg border border-dashed border-border bg-surface-subtle px-2.5 py-2 text-xs text-muted-light">
        No parameters
      </div>
    );
  }

  return (
    <div className="mt-2 grid gap-2">
      <div className="text-[11px] font-semibold uppercase tracking-[0.06em] text-muted-light">
        Parameters
      </div>
      {formatted}
      <details className="rounded-lg border border-border bg-surface-subtle" data-no-tool-select>
        <summary className="cursor-pointer px-2.5 py-1.5 text-[11px] font-semibold text-muted-light">
          raw
        </summary>
        <pre className="max-h-32 overflow-auto border-t border-border p-2 text-[11px] leading-5 text-muted">
          {formatPayload(parameters)}
        </pre>
      </details>
    </div>
  );
}

export type ToolResultVariant = "chat" | "console";

type MetaChip = { label: string; value: string };

function parseResultRecord(value: unknown): Record<string, unknown> | null {
  return parseToolResultRecord(value);
}

function observationText(value: unknown): string {
  const normalized = unwrapToolResultPayload(value);
  if (typeof normalized === "string") return normalized;
  const record = parseResultRecord(normalized);
  if (record && typeof record.observation === "string") return record.observation;
  return toolResultObservationText(value);
}

export function formatPayload(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
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

function MetaChips({ items }: { items: MetaChip[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <span
          key={item.label}
          className="inline-flex items-center gap-1 rounded-full border border-border bg-surface px-2 py-0.5 text-[10px] text-muted"
        >
          <span className="font-semibold text-muted-light">{item.label}</span>
          <span className="max-w-[200px] truncate font-mono text-muted">{item.value}</span>
        </span>
      ))}
    </div>
  );
}

function isNumericCell(value: unknown): boolean {
  if (typeof value === "number") return true;
  if (typeof value !== "string") return false;
  return value.trim() !== "" && !Number.isNaN(Number(value));
}

function ResultDataTable({
  columns,
  rows,
  variant,
}: {
  columns: string[];
  rows: unknown[][];
  variant: ToolResultVariant;
}) {
  const previewRows = rows.slice(0, variant === "console" ? 100 : 50);
  const shellClass =
    variant === "console"
      ? `${dataTableShellClass} max-h-[min(480px,60vh)] overflow-auto`
      : `${dataTableShellClass} overflow-auto`;

  const numericColumns = columns.map((_, index) =>
    previewRows.some((row) => isNumericCell(row[index])),
  );

  return (
    <div className={shellClass}>
      <table className={dataTableClass}>
        <thead className={dataTableHeadClass}>
          <tr>
            {columns.map((column, index) => (
              <th
                key={column}
                className={[
                  "whitespace-nowrap px-2.5 py-1.5 font-semibold",
                  numericColumns[index] ? "text-right" : "",
                ].join(" ")}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {previewRows.map((row, rowIndex) => (
            <tr key={rowIndex} className={dataTableRowClass}>
              {columns.map((_, cellIndex) => (
                <td
                  key={cellIndex}
                  className={[
                    numericColumns[cellIndex]
                      ? dataTableCellNumericClass
                      : dataTableCellClass,
                    cellIndex === 0 && !numericColumns[cellIndex]
                      ? "font-medium text-foreground"
                      : "",
                    row[cellIndex] === null || row[cellIndex] === undefined
                      ? "text-muted-light"
                      : "",
                  ].join(" ")}
                >
                  {formatCell(row[cellIndex])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > previewRows.length ? (
        <div className="border-t border-border bg-surface-subtle px-2.5 py-1 text-[10px] text-muted-light">
          Showing the first {previewRows.length} rows of {rows.length}.
        </div>
      ) : null}
    </div>
  );
}

function TextBlock({
  text,
  variant,
  tone = "default",
}: {
  text: string;
  variant: ToolResultVariant;
  tone?: "default" | "terminal";
}) {
  const className =
    tone === "terminal"
      ? variant === "console"
        ? "max-h-80 overflow-auto rounded-lg bg-code-bg p-3 font-mono text-xs leading-5 text-slate-100"
        : "max-h-44 overflow-auto rounded-lg bg-code-bg p-2 font-mono text-xs leading-5 text-slate-100"
      : variant === "console"
        ? "max-h-80 overflow-auto rounded-lg bg-surface-subtle p-3 text-xs leading-5 text-muted"
        : "max-h-44 overflow-auto rounded-lg bg-surface p-2 text-xs leading-5 text-muted";

  return <pre className={`whitespace-pre-wrap ${className}`}>{text}</pre>;
}

function TaskStatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const toneClass =
    normalized === "completed"
      ? statusTone("success")
      : normalized === "in_progress" || normalized === "in progress"
        ? statusTone("info")
        : normalized === "failed" || normalized === "cancelled"
          ? statusTone("error")
          : statusTone("muted");
  const label =
    normalized === "completed"
      ? "Completed"
      : normalized === "in_progress" || normalized === "in progress"
        ? "In progress"
        : normalized === "pending"
          ? "Pending"
          : status;

  return (
    <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${toneClass}`}>
      {label}
    </span>
  );
}

function renderListDataSources(record: Record<string, unknown>, variant: ToolResultVariant): ReactNode {
  const datasources = Array.isArray(record.datasources) ? record.datasources : [];
  if (datasources.length === 0) return null;

  return (
    <div className="grid gap-2">
      {datasources.map((item, index) => {
        const ds = item as Record<string, unknown>;
        const id = typeof ds.id === "string" ? ds.id : `source-${index}`;
        const name = typeof ds.name === "string" ? ds.name : id;
        const type = typeof ds.type === "string" ? ds.type : undefined;
        const status = typeof ds.status === "string" ? ds.status : undefined;
        const description = typeof ds.description === "string" ? ds.description : undefined;
        return (
          <div
            key={id}
            className="rounded-lg border border-border bg-surface/80 p-2.5"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs font-semibold text-foreground">{name}</span>
              {type ? (
                <span className="rounded border border-border bg-surface-subtle px-1.5 py-0.5 font-mono text-[10px] text-muted">
                  {type}
                </span>
              ) : null}
              {status ? <TaskStatusBadge status={status} /> : null}
            </div>
            <div className="mt-1 font-mono text-[10px] text-muted-light">{id}</div>
            {description ? (
              <p className="mt-1.5 text-[11px] leading-4 text-muted">{description}</p>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function renderSqlResult(result: unknown, variant: ToolResultVariant): ReactNode | null {
  const parsed = parseSqlToolResult(result);
  if (!parsed || parsed.columns.length === 0) return null;

  const { columns: displayColumns, rows } = normalizeSqlTable(parsed.columns, parsed.rows);
  const metaItems = [
    {
      label: "Rows",
      value: String(parsed.row_count ?? parsed.rows.length),
    },
    ...(parsed.elapsed_ms !== undefined
      ? [{ label: "Duration", value: `${parsed.elapsed_ms}ms` }]
      : []),
    ...(parsed.audit_log_id ? [{ label: "Audit", value: parsed.audit_log_id }] : []),
    ...(parsed.artifact_id ? [{ label: "Output", value: parsed.artifact_id }] : []),
  ];

  if (rows.length === 0) {
    return (
      <div className="grid gap-2">
        <MetaChips items={metaItems} />
        <p className="rounded-lg border border-dashed border-border bg-surface-subtle px-2.5 py-2 text-xs text-muted-light">
          The query returned no rows.
        </p>
      </div>
    );
  }

  return (
    <div className="grid gap-2">
      <MetaChips items={metaItems} />
      <ResultDataTable columns={displayColumns} rows={rows} variant={variant} />
    </div>
  );
}

function renderSchemaResult(result: unknown): ReactNode | null {
  const parsed = parseSchemaToolResult(result);
  if (!parsed?.tables?.length) return null;

  return (
    <div className="grid gap-2">
      {parsed.datasource_id ? (
        <MetaChips items={[{ label: "Datasource", value: parsed.datasource_id }]} />
      ) : null}
      {parsed.tables.map((table) => (
        <div key={table.name} className="rounded-lg border border-border bg-surface/80 p-2.5">
          <div className="font-mono text-xs font-semibold text-foreground">{table.name}</div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {(table.columns ?? []).map((column) => (
              <span
                key={column.name}
                className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted"
                title={column.type}
              >
                {column.name}
                {column.type ? <span className="text-muted-light"> · {column.type}</span> : null}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function renderPreviewTable(record: Record<string, unknown>, variant: ToolResultVariant): ReactNode {
  const columns = Array.isArray(record.columns)
    ? record.columns.filter((column): column is string => typeof column === "string")
    : [];
  const rawRows = Array.isArray(record.rows) ? record.rows : [];
  if (columns.length === 0 || rawRows.length === 0) return null;

  const { columns: displayColumns, rows } = normalizeSqlTable(columns, rawRows);
  const table = typeof record.table === "string" ? record.table : undefined;
  const rowCount =
    typeof record.row_count === "number" ? record.row_count : rawRows.length;

  return (
    <div className="grid gap-2">
      <MetaChips
        items={[
          ...(table ? [{ label: "Table", value: table }] : []),
          { label: "Rows", value: String(rowCount) },
        ]}
      />
      <ResultDataTable columns={displayColumns} rows={rows} variant={variant} />
    </div>
  );
}

function renderWorkspaceObservation(toolName: string, text: string, variant: ToolResultVariant): ReactNode | null {
  const writeMatch = text.match(/^Wrote (\d+) bytes to (.+)$/u);
  if (writeMatch) {
    return (
      <div className="grid gap-2">
        <MetaChips
          items={[
            { label: "Path", value: writeMatch[2] },
            { label: "Size", value: `${writeMatch[1]} bytes` },
          ]}
        />
        <p className="text-xs text-step-success">File written to the workspace.</p>
      </div>
    );
  }

  const mkdirMatch = text.match(/^Created directory (.+)$/u);
  if (mkdirMatch) {
    return (
      <div className="grid gap-2">
        <MetaChips items={[{ label: "Directory", value: mkdirMatch[1] }]} />
        <p className="text-xs text-muted">Directory created.</p>
      </div>
    );
  }

  const statMatch = text.match(/^(.+?) Type: (\w+) Size: (\d+) bytes Modified: (.+)$/u);
  if (statMatch && toolName === "file_stat") {
    return (
      <MetaChips
        items={[
          { label: "Path", value: statMatch[1] },
          { label: "Type", value: statMatch[2] },
          { label: "Size", value: `${statMatch[3]} bytes` },
          { label: "Modified", value: statMatch[4] },
        ]}
      />
    );
  }

  if (toolName === "grep") {
    const lines = text.split("\n").filter(Boolean);
    const header = lines[0] ?? "";
    const matches = lines.slice(1).filter((line) => line !== "---");
    if (matches.length > 0 || header.includes("match")) {
      return (
        <div className="grid gap-2">
          {header ? <p className="text-xs font-semibold text-muted">{header}</p> : null}
          <div className="grid gap-1">
            {matches.map((line, index) => (
              <div
                key={index}
                className="rounded border border-border bg-surface px-2 py-1 font-mono text-[10px] text-muted"
              >
                {line}
              </div>
            ))}
          </div>
        </div>
      );
    }
  }

  if (toolName === "list_files") {
    const lines = text.split("\n").filter((line) => line.trim().length > 0);
    if (lines.length > 0) {
      return (
        <div className="grid gap-1">
          {lines.map((line, index) => (
            <div key={index} className="font-mono text-[10px] text-muted">
              {line}
            </div>
          ))}
        </div>
      );
    }
  }

  if (toolName === "read_file" || toolName === "edit_file") {
    if (text.includes("→") || text.includes("\n")) {
      return <TextBlock text={text} variant={variant} />;
    }
  }

  if (toolName === "execute_command") {
    const trimmed = text.trim();
    if (trimmed.length > 0) {
      return <TextBlock text={text} variant={variant} tone="terminal" />;
    }
    return (
      <p className="text-xs text-muted-light">Command ran with no stdout.</p>
    );
  }

  return null;
}

function renderTaskTools(record: Record<string, unknown>, variant: ToolResultVariant): ReactNode | null {
  const tasks = Array.isArray(record.tasks) ? record.tasks : [];
  const summary = record.summary as Record<string, unknown> | undefined;
  const content = typeof record.content === "string" ? record.content : undefined;

  if (tasks.length === 0 && !content) return null;

  const total = typeof summary?.total === "number" ? summary.total : tasks.length;
  const completed = typeof summary?.completed === "number" ? summary.completed : undefined;
  const allCompleted = summary?.allCompleted === true;

  return (
    <div className="grid gap-2">
      {content ? <TextBlock text={content} variant={variant} /> : null}
      {total > 0 ? (
        <MetaChips
          items={[
            { label: "Tasks", value: String(total) },
            ...(completed !== undefined ? [{ label: "Completed", value: String(completed) }] : []),
            ...(allCompleted ? [{ label: "Status", value: "All completed" }] : []),
          ]}
        />
      ) : null}
      {tasks.length > 0 ? (
        <div className="grid gap-1.5">
          {tasks.map((task, index) => {
            const item = task as Record<string, unknown>;
            const id = typeof item.id === "string" ? item.id : `task-${index}`;
            const taskContent = typeof item.content === "string" ? item.content : id;
            const status = typeof item.status === "string" ? item.status : "pending";
            return (
              <div
                key={id}
                className="flex items-start justify-between gap-2 rounded-lg border border-border bg-surface/80 px-2.5 py-2"
              >
                <span className="text-xs text-muted">{taskContent}</span>
                <TaskStatusBadge status={status} />
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

function renderCollaboration(record: Record<string, unknown>, variant: ToolResultVariant): ReactNode | null {
  const content = typeof record.content === "string" ? record.content : undefined;
  if (!content) return null;
  return <TextBlock text={content} variant={variant} />;
}

function renderKnowledge(record: Record<string, unknown>, variant: ToolResultVariant): ReactNode | null {
  const chunks = Array.isArray(record.chunks) ? record.chunks : [];
  const collectionId =
    typeof record.collection_id === "string" ? record.collection_id : undefined;
  if (chunks.length === 0 && !collectionId) return null;

  return (
    <div className="grid gap-2">
      {collectionId ? (
        <MetaChips items={[{ label: "Knowledge base", value: collectionId }]} />
      ) : null}
      {chunks.map((chunk, index) => {
        const item = chunk as Record<string, unknown>;
        const text =
          typeof item.text === "string"
            ? item.text
            : typeof item.content === "string"
              ? item.content
              : formatPayload(item);
        const source =
          typeof item.source === "string"
            ? item.source
            : typeof item.document_id === "string"
              ? item.document_id
              : undefined;
        return (
          <div
            key={index}
            className="rounded-lg border border-border bg-surface/80 p-2.5"
          >
            {source ? (
              <div className="mb-1 font-mono text-[10px] text-muted-light">{source}</div>
            ) : null}
            <p className="text-xs leading-5 text-muted">{text}</p>
          </div>
        );
      })}
    </div>
  );
}

export function renderFormattedToolResult(
  toolName: string,
  result: unknown,
  variant: ToolResultVariant,
): ReactNode | null {
  const record = parseResultRecord(result);

  if (toolName === "list_data_sources" && record) {
    return renderListDataSources(record, variant);
  }

  if (toolName === "preview_table" && record) {
    return renderPreviewTable(record, variant);
  }

  if (toolName === "run_sql_readonly") {
    return renderSqlResult(result, variant);
  }

  if (toolName === "inspect_schema") {
    return renderSchemaResult(result);
  }

  if (
    (toolName === "task_write" ||
      toolName === "task_update" ||
      toolName === "task_check" ||
      toolName === "task_complete") &&
    record
  ) {
    return renderTaskTools(record, variant);
  }

  if ((toolName === "ask_user" || toolName === "submit_plan") && record) {
    return renderCollaboration(record, variant);
  }

  if (toolName === "retrieve_knowledge" && record) {
    return renderKnowledge(record, variant);
  }

  const observation = observationText(result);
  if (
    toolName === "read_file" ||
    toolName === "write_file" ||
    toolName === "edit_file" ||
    toolName === "list_files" ||
    toolName === "grep" ||
    toolName === "file_stat" ||
    toolName === "mkdir" ||
    toolName === "execute_command"
  ) {
    const workspaceView = renderWorkspaceObservation(toolName, observation, variant);
    if (workspaceView) return workspaceView;
    if (observation) return <TextBlock text={observation} variant={variant} />;
  }

  return null;
}

export function ToolFailureResult({
  toolName,
  result,
}: {
  toolName: string;
  result?: string;
}) {
  const failure = resolveToolFailurePresentation(result);
  return (
    <div className="rounded-xl border border-step-error/25 bg-step-error/8 p-3 text-xs leading-5 text-step-error">
      <div className="font-semibold text-step-error">{failure.title}</div>
      <p className="mt-1">
        <span className="font-mono">{toolName}</span>: {failure.message}
      </p>
      {failure.hint ? <p className="mt-2 text-[11px] text-step-error/90">{failure.hint}</p> : null}
    </div>
  );
}

export function ToolRawFallback({
  result,
  variant = "chat",
  collapsible = true,
}: {
  result: unknown;
  variant?: ToolResultVariant;
  collapsible?: boolean;
}) {
  const payload = formatPayload(result).trim();
  const preClass =
    variant === "console"
      ? "max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-subtle p-3 font-mono text-xs leading-5 text-muted"
      : "max-h-44 overflow-auto whitespace-pre-wrap rounded-lg bg-surface p-2 text-xs leading-5 text-muted";

  if (!payload) {
    return (
      <p className="rounded-lg border border-dashed border-border bg-surface-subtle px-2.5 py-2 text-xs text-muted-light">
        The tool returned no visible output.
      </p>
    );
  }

  if (!collapsible) {
    return <pre className={preClass}>{payload}</pre>;
  }

  return (
    <details className="rounded-lg border border-border bg-surface-subtle" data-no-tool-select>
      <summary className="cursor-pointer px-2.5 py-1.5 text-[11px] font-semibold text-muted-light">
        Raw response
      </summary>
      <pre className={`border-t border-border ${preClass}`}>{payload}</pre>
    </details>
  );
}

export function ToolFormattedResult({
  toolName,
  result,
  variant = "chat",
  showRawFallback = true,
}: {
  toolName: string;
  result: unknown;
  variant?: ToolResultVariant;
  showRawFallback?: boolean;
}) {
  const formatted = renderFormattedToolResult(toolName, result, variant);
  if (!formatted) {
    return showRawFallback ? (
      <ToolRawFallback result={result} variant={variant} collapsible={false} />
    ) : null;
  }

  return (
    <div className="grid gap-2">
      {formatted}
      {showRawFallback ? (
        <ToolRawFallback result={result} variant={variant} collapsible />
      ) : null}
    </div>
  );
}
