"use client";

import { useEffect, useState } from "react";
import { useT } from "../../../i18n/locale-context";
import type { TranslateFn } from "../../../i18n/types";
import { configApi } from "../../../lib/config-api";
import type { DatasourceSchemaDto } from "../../../lib/config-api";
import { btnSecondaryClass, panelTitleClass, sectionLabelClass } from "../ui-tokens";

type DatasourceSchemaPreviewPopoverProps = {
  datasourceId: string;
  datasourceName: string;
  onClose: () => void;
};

function formatStats(table: DatasourceSchemaDto["tables"][number], t: TranslateFn): string {
  const stats = table.stats;
  if (!stats) return "";
  const parts = [
    stats.rowCount !== undefined ? t("schema.rows", { count: stats.rowCount.toLocaleString() }) : "",
    stats.sizeBytes !== undefined ? `${stats.sizeBytes.toLocaleString()} B` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

async function copyText(value: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard) {
    await navigator.clipboard.writeText(value);
  }
}

export function DatasourceSchemaPreviewPopover({
  datasourceId,
  datasourceName,
  onClose,
}: DatasourceSchemaPreviewPopoverProps) {
  const t = useT();
  const [schema, setSchema] = useState<DatasourceSchemaDto | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (!datasourceId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSchema(null);

    void configApi
      .getDatasourceSchema(datasourceId, {
        includeStats: true,
      })
      .then((next) => {
        if (!cancelled) {
          setSchema(next);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t("schema.loadFailed"));
          setSchema(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [datasourceId, t]);

  const handleCopy = async (value: string) => {
    await copyText(value);
    setCopied(value);
    window.setTimeout(() => setCopied((current) => (current === value ? null : current)), 1200);
  };

  return (
    <section
      className="absolute left-0 top-full z-50 mt-2 flex max-h-[min(560px,calc(100vh-96px))] w-[min(720px,calc(100vw-2rem))] flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-2xl"
      role="dialog"
      aria-labelledby="datasource-schema-preview-title"
    >
      <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h3
            id="datasource-schema-preview-title"
            className={`${panelTitleClass} flex min-w-0 flex-wrap items-center gap-2`}
          >
            <span className="truncate">{datasourceName}</span>
            <span className="shrink-0 rounded-full border border-border bg-surface-subtle px-1.5 py-0.5 text-[10px] font-medium text-muted">
              {t("schema.preview")}
            </span>
          </h3>
          <p className="mt-1 text-xs text-muted-light">
            {t("schema.previewHelp")}
          </p>
        </div>
        <button type="button" onClick={onClose} className={btnSecondaryClass}>
          {t("common.close")}
        </button>
      </div>

      <div className="min-h-0 overflow-y-auto p-4">
        {loading ? (
          <p className="mb-3 text-xs text-muted-light">{t("schema.loading")}</p>
        ) : null}

        {error ? (
          <p className="mb-3 rounded-lg bg-step-error/10 px-2.5 py-2 text-xs text-step-error">
            {error}
          </p>
        ) : null}

        <div className="grid gap-2">
          {schema?.tables?.length ? (
            schema.tables.map((table) => {
              const tableName = table.table ?? table.name;
              return (
                <div key={tableName} className="rounded-lg border border-border bg-surface-subtle p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-xs font-semibold text-foreground">
                        {tableName}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-light">
                        {table.sampleAvailable ? <span>{t("schema.sampleAvailable")}</span> : null}
                        {formatStats(table, t) ? <span>{formatStats(table, t)}</span> : null}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => void handleCopy(tableName)}
                      className={btnSecondaryClass}
                    >
                      {copied === tableName ? t("common.copied") : t("schema.copyTableName")}
                    </button>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {table.columns.map((column) => {
                      const columnRef = `${tableName}.${column.name}`;
                      return (
                        <button
                          key={columnRef}
                          type="button"
                          onClick={() => void handleCopy(columnRef)}
                          className="rounded-full border border-border bg-surface px-2 py-1 text-[11px] text-muted transition hover:border-primary-light/40 hover:text-foreground"
                          title={column.description}
                        >
                          {column.name}
                          {column.type ? (
                            <span className="text-muted-light"> · {column.type}</span>
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            })
          ) : (
            <p className="text-xs text-muted-light">
              {schema ? t("schema.empty") : null}
            </p>
          )}
        </div>

        <div className="mt-3">
          <span className={sectionLabelClass}>
            {t("schema.pasteHint")}
          </span>
        </div>
      </div>
    </section>
  );
}
