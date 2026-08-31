"use client";

import { useEffect, useMemo, useState } from "react";
import { configApi } from "../../../lib/config-api";
import type {
  DatasourceSchemaDto,
  DatasourceSchemaTableDto,
  DatasourceTablePreviewDto,
} from "../../../lib/config-api";
import { useT } from "../../../i18n/locale-context";
import type { TranslateFn } from "../../../i18n/types";
import { normalizeSqlTable } from "../table-rows";
import type { WorkspaceConfigItem } from "../data-task-state";
import {
  summarizeDatasourceConnection,
} from "../datasource-metadata";
import { DatasourceTypeIcon } from "./DatasourceTypeIcon";
import { btnSecondaryClass } from "../ui-tokens";

type DatasourceExplorerPanelProps = {
  item: WorkspaceConfigItem;
  onBack: () => void;
  onEdit: () => void;
  onTest?: () => Promise<void>;
  onIntrospect?: () => Promise<void>;
};

type ExplorerTab = "columns" | "data" | "info";

function tableNameOf(table: DatasourceSchemaTableDto): string {
  return table.table ?? table.name;
}

function formatStats(table: DatasourceSchemaTableDto | null | undefined, t: TranslateFn): string {
  const stats = table?.stats;
  if (!stats) return t("explorer.noStats");
  return [
    stats.rowCount !== undefined ? t("explorer.rows", { count: stats.rowCount.toLocaleString() }) : "",
    stats.sizeBytes !== undefined ? `${stats.sizeBytes.toLocaleString()} B` : "",
  ].filter(Boolean).join(" · ") || t("explorer.noStats");
}

function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function nonSqlNotice(type: string | undefined, t: TranslateFn): string | null {
  if (type === "mongodb") return t("explorer.noticeMongodb");
  if (type === "redis") return t("explorer.noticeRedis");
  if (type === "elasticsearch" || type === "opensearch") {
    return t("explorer.noticeSearch");
  }
  return null;
}

export function DatasourceExplorerPanel({
  item,
  onBack,
  onEdit,
  onTest,
  onIntrospect,
}: DatasourceExplorerPanelProps) {
  const t = useT();
  const [schema, setSchema] = useState<DatasourceSchemaDto | null>(null);
  const [query, setQuery] = useState("");
  const [selectedTableName, setSelectedTableName] = useState("");
  const [activeTab, setActiveTab] = useState<ExplorerTab>("columns");
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [preview, setPreview] = useState<DatasourceTablePreviewDto | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  const settings = item.settings ?? {};
  const type = settings.type ?? "unknown";
  const notice = nonSqlNotice(type, t);

  const filteredTables = useMemo(() => {
    const tables = schema?.tables ?? [];
    const needle = query.trim().toLowerCase();
    if (!needle) return tables;
    return tables.filter((table) =>
      [
        tableNameOf(table),
        table.description ?? "",
        ...table.columns.map((column) => `${column.name} ${column.type ?? ""}`),
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [query, schema]);

  const selectedTable =
    filteredTables.find((table) => tableNameOf(table) === selectedTableName) ??
    filteredTables[0] ??
    null;
  const selectedName = selectedTable ? tableNameOf(selectedTable) : "";

  const loadSchema = async () => {
    setSchemaLoading(true);
    setSchemaError(null);
    try {
      const next = await configApi.getDatasourceSchema(item.id, {
        q: query.trim() || undefined,
        includeStats: true,
      });
      setSchema(next);
      const firstName = next.tables[0] ? tableNameOf(next.tables[0]) : "";
      setSelectedTableName((current) =>
        current && next.tables.some((table) => tableNameOf(table) === current)
          ? current
          : firstName,
      );
    } catch (error) {
      setSchemaError(error instanceof Error ? error.message : t("explorer.loadSchemaFailed"));
    } finally {
      setSchemaLoading(false);
    }
  };

  const loadPreview = async () => {
    if (!selectedName) return;
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const next = await configApi.getDatasourceTablePreview(item.id, selectedName, {
        limit: 50,
        offset: 0,
      });
      setPreview(next);
    } catch (error) {
      setPreview(null);
      setPreviewError(
        error instanceof Error
          ? error.message
          : t("explorer.previewApiUnavailable"),
      );
    } finally {
      setPreviewLoading(false);
    }
  };

  useEffect(() => {
    void loadSchema();
    // Load once per datasource. Search uses the explicit Search/Refresh button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item.id]);

  const previewTable = preview
    ? normalizeSqlTable(preview.columns.map((column) => column.name), preview.rows)
    : null;

  return (
    <section className="flex min-h-[640px] flex-col overflow-hidden rounded-2xl border border-border bg-white shadow-[var(--shadow-card)]">
      <header className="flex flex-wrap items-center gap-3 border-b border-border bg-slate-50 px-4 py-3">
        <button type="button" onClick={onBack} className={btnSecondaryClass}>
          {t("common.back")}
        </button>
        <DatasourceTypeIcon
          typeName={type}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border"
          iconClassName="h-6 w-6 object-contain"
        />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-base font-semibold text-slate-950">{item.name}</h3>
          <p className="truncate text-xs text-slate-500">
            {type} · {summarizeDatasourceConnection(item)}
          </p>
        </div>
        <span className="rounded-full border border-border bg-white px-2.5 py-1 text-[11px] font-medium text-slate-500">
          {item.status ?? t("explorer.untested")}
        </span>
        {onTest ? (
          <button
            type="button"
            disabled={actionBusy}
            onClick={() => {
              setActionBusy(true);
              void onTest().finally(() => setActionBusy(false));
            }}
            className={`${btnSecondaryClass} disabled:opacity-50`}
          >
            {t("common.test")}
          </button>
        ) : null}
        {onIntrospect ? (
          <button
            type="button"
            disabled={actionBusy}
            onClick={() => {
              setActionBusy(true);
              void onIntrospect()
                .then(loadSchema)
                .finally(() => setActionBusy(false));
            }}
            className={`${btnSecondaryClass} disabled:opacity-50`}
          >
            {t("configPanel.syncSchema")}
          </button>
        ) : null}
        <button type="button" onClick={onEdit} className={btnSecondaryClass}>
          {t("common.edit")}
        </button>
      </header>

      {notice ? (
        <div className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">
          {notice}
        </div>
      ) : null}

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="min-h-0 border-b border-border bg-slate-50/70 p-3 lg:border-b-0 lg:border-r">
          <div className="flex gap-2">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("explorer.searchPlaceholder")}
                className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-white px-3 text-xs text-slate-900 outline-none focus:border-primary-light"
            />
            <button
              type="button"
              onClick={() => void loadSchema()}
              disabled={schemaLoading}
              className={`${btnSecondaryClass} disabled:opacity-50`}
            >
              {schemaLoading ? "..." : t("common.search")}
            </button>
          </div>
          {schemaError ? (
            <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
              {schemaError}
            </p>
          ) : null}
          <div className="mt-3 max-h-[520px] space-y-1 overflow-y-auto">
            {filteredTables.length === 0 ? (
              <p className="rounded-lg border border-dashed border-border bg-white p-3 text-xs text-slate-500">
                {schema ? t("explorer.noMatchingObjects") : t("explorer.loadSchemaHint")}
              </p>
            ) : (
              filteredTables.map((table) => {
                const name = tableNameOf(table);
                const active = name === selectedName;
                return (
                  <button
                    key={name}
                    type="button"
                    onClick={() => {
                      setSelectedTableName(name);
                      setPreview(null);
                      setPreviewError(null);
                    }}
                    className={[
                      "w-full cursor-pointer rounded-lg border px-3 py-2 text-left transition-colors duration-150",
                            active
                              ? "border-primary-light/30 bg-primary-light/10 text-primary"
                        : "border-transparent bg-white text-slate-700 hover:border-border hover:bg-slate-50",
                    ].join(" ")}
                  >
                    <span className="block truncate text-xs font-semibold">{name}</span>
                    <span className="mt-0.5 block truncate text-[10px] text-slate-500">
                      {t("explorer.columnsMeta", {
                        count: table.columns.length,
                        stats: formatStats(table, t),
                      })}
                    </span>
                  </button>
                );
              })
            )}
          </div>
        </aside>

        <main className="min-w-0 overflow-hidden p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <h4 className="truncate text-sm font-semibold text-slate-950">
                {selectedName || t("explorer.noObjectSelected")}
              </h4>
              <p className="text-xs text-slate-500">{formatStats(selectedTable, t)}</p>
            </div>
            <div className="inline-flex rounded-lg border border-border bg-slate-50 p-0.5">
              {([
                ["columns", t("explorer.tabColumns")],
                ["data", t("explorer.tabData")],
                ["info", t("explorer.tabInfo")],
              ] as const).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setActiveTab(id)}
                  className={[
                    "cursor-pointer rounded-md px-3 py-1.5 text-xs font-medium transition-colors duration-150",
                    activeTab === id
                      ? "bg-white text-slate-950 shadow-sm"
                      : "text-slate-500 hover:text-slate-800",
                  ].join(" ")}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>

          {activeTab === "columns" ? (
            <div className="overflow-auto rounded-xl border border-border">
              <table className="w-full min-w-[640px] text-left text-xs">
                <thead className="bg-slate-50 text-slate-500">
                  <tr>
                    <th className="px-3 py-2 font-semibold">{t("explorer.column")}</th>
                    <th className="px-3 py-2 font-semibold">{t("explorer.type")}</th>
                    <th className="px-3 py-2 font-semibold">{t("explorer.nullable")}</th>
                    <th className="px-3 py-2 font-semibold">{t("explorer.description")}</th>
                  </tr>
                </thead>
                <tbody>
                  {(selectedTable?.columns ?? []).map((column) => (
                            <tr key={column.name} className="border-t border-border hover:bg-primary-light/5">
                      <td className="px-3 py-2 font-mono font-medium text-slate-900">{column.name}</td>
                      <td className="px-3 py-2 font-mono text-slate-600">{column.type || "-"}</td>
                      <td className="px-3 py-2 text-slate-500">{column.nullable === false ? t("common.no") : t("common.yes")}</td>
                      <td className="px-3 py-2 text-slate-500">{column.description || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          {activeTab === "data" ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-primary-light/20 bg-primary-light/5 px-3 py-2">
                <p className="text-xs text-primary">
                  {t("explorer.previewHint")}
                </p>
                <button
                  type="button"
                  onClick={() => void loadPreview()}
                  disabled={!selectedName || previewLoading}
                  className={`${btnSecondaryClass} bg-white disabled:opacity-50`}
                >
                  {previewLoading ? t("explorer.loadingPreview") : t("explorer.loadRows")}
                </button>
              </div>
              {previewError ? (
                <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs leading-5 text-amber-900">
                  {previewError}
                  <div className="mt-1 font-medium">{t("explorer.previewEndpointPending")}</div>
                </div>
              ) : null}
              {previewTable ? (
                <div className="overflow-auto rounded-xl border border-border">
                  <table className="w-full min-w-max text-left text-[11px]">
                    <thead className="bg-slate-50 text-slate-500">
                      <tr>
                        {previewTable.columns.map((column) => (
                          <th key={column} className="px-2.5 py-2 font-semibold">
                            {column}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {previewTable.rows.map((row, rowIndex) => (
                          <tr key={rowIndex} className="border-t border-border hover:bg-primary-light/5">
                          {row.map((cell, cellIndex) => (
                            <td key={cellIndex} className="whitespace-nowrap px-2.5 py-1.5 text-slate-600">
                              {cellText(cell)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <div className="border-t border-border bg-slate-50 px-3 py-2 text-[11px] text-slate-500">
                    {preview?.total !== undefined
                      ? t("explorer.totalRows", { count: preview.total.toLocaleString() })
                      : t("explorer.totalUnknown")}
                    {preview?.hasMore ? t("explorer.moreRows") : ""}
                  </div>
                </div>
              ) : previewError ? null : (
                <div className="rounded-xl border border-dashed border-border bg-slate-50 p-6 text-center text-sm text-slate-500">
                  {t("explorer.previewEmpty")}
                </div>
              )}
            </div>
          ) : null}

          {activeTab === "info" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                [t("explorer.infoDatasourceId"), item.id],
                [t("explorer.infoType"), type],
                [t("explorer.infoConnection"), summarizeDatasourceConnection(item)],
                [t("explorer.infoStatus"), item.status ?? t("explorer.untested")],
                [t("explorer.infoEnabled"), item.enabled ? t("common.yes") : t("common.no")],
                [t("explorer.infoSelectedObject"), selectedName || "-"],
                [t("explorer.infoSampleAvailable"), selectedTable?.sampleAvailable ? t("common.yes") : t("common.no")],
                [t("explorer.infoInspectedAt"), schema?.inspectedAt ?? "-"],
              ].map(([label, value]) => (
                <div key={label} className="rounded-xl border border-border bg-slate-50 px-3 py-2.5">
                  <dt className="text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">
                    {label}
                  </dt>
                  <dd className="mt-1 break-all text-sm text-slate-800">{value}</dd>
                </div>
              ))}
            </div>
          ) : null}
        </main>
      </div>
    </section>
  );
}
