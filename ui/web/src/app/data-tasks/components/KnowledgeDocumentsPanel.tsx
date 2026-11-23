"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { KnowledgeDocumentDto } from "../../../lib/config-api";
import { useT } from "../../../i18n/locale-context";
import type { WorkspaceConfigItem } from "../data-task-state";
import { btnSecondaryClass } from "../ui-tokens";

type KnowledgeDocumentsPanelProps = {
  item: WorkspaceConfigItem;
  onBack: () => void;
  onEdit: () => void;
  onUpload: (file: File) => Promise<void>;
  onList: () => Promise<{ documents: KnowledgeDocumentDto[] }>;
  onReindex?: () => Promise<void>;
  onDeleteDocument?: (documentId: string) => Promise<void>;
  onRetryDocument?: (documentId: string) => Promise<void>;
};

function statusClass(status: string): string {
  if (status === "ready") return "bg-emerald-50 text-emerald-700";
  if (status === "failed") return "bg-rose-50 text-rose-700";
  if (status === "indexing" || status === "parsing" || status === "uploaded") {
    return "bg-amber-50 text-amber-700";
  }
  return "bg-slate-100 text-slate-500";
}

function knowledgeDocStatusLabel(
  status: string,
  t: ReturnType<typeof useT>,
): string {
  switch (status) {
    case "ready":
      return t("configPanel.knowledgeDocStatus.ready");
    case "failed":
      return t("configPanel.knowledgeDocStatus.failed");
    case "indexing":
      return t("configPanel.knowledgeDocStatus.indexing");
    case "parsing":
      return t("configPanel.knowledgeDocStatus.parsing");
    case "uploaded":
      return t("configPanel.knowledgeDocStatus.uploaded");
    default:
      return status;
  }
}

export function KnowledgeDocumentsPanel({
  item,
  onBack,
  onEdit,
  onUpload,
  onList,
  onReindex,
  onDeleteDocument,
  onRetryDocument,
}: KnowledgeDocumentsPanelProps) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [documents, setDocuments] = useState<KnowledgeDocumentDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const [rowBusyId, setRowBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await onList();
      setDocuments(result.documents);
    } catch (listError) {
      setError(listError instanceof Error ? listError.message : t("configPanel.knowledgeLoadFailed"));
      setDocuments([]);
    } finally {
      setLoading(false);
    }
  }, [onList, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleUpload = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await onUpload(file);
      await refresh();
    } catch (uploadError) {
      setError(
        uploadError instanceof Error ? uploadError.message : t("configPanel.knowledgeUploadFailed"),
      );
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (documentId: string) => {
    if (!onDeleteDocument) return;
    if (!window.confirm(t("configPanel.knowledgeDeleteConfirm"))) return;
    setRowBusyId(documentId);
    setError(null);
    try {
      await onDeleteDocument(documentId);
      await refresh();
    } catch (deleteError) {
      setError(
        deleteError instanceof Error ? deleteError.message : t("configPanel.knowledgeDeleteFailed"),
      );
    } finally {
      setRowBusyId(null);
    }
  };

  const handleRetry = async (documentId: string) => {
    if (!onRetryDocument) return;
    setRowBusyId(documentId);
    setError(null);
    try {
      await onRetryDocument(documentId);
      await refresh();
    } catch (retryError) {
      setError(
        retryError instanceof Error ? retryError.message : t("configPanel.knowledgeRetryFailed"),
      );
    } finally {
      setRowBusyId(null);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-950">{item.name}</h3>
          <p className="mt-1 font-mono text-[10px] text-slate-400">{item.id}</p>
          <p className="mt-2 text-xs text-slate-600">{t("configPanel.knowledgeDocumentsHelp")}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={onBack} className={btnSecondaryClass}>
            {t("common.back")}
          </button>
          <button type="button" onClick={onEdit} className={btnSecondaryClass}>
            {t("common.edit")}
          </button>
          {onReindex ? (
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => {
                setActionBusy(true);
                setError(null);
                void onReindex()
                  .then(() => refresh())
                  .catch((reindexError: unknown) => {
                    setError(
                      reindexError instanceof Error
                        ? reindexError.message
                        : t("configPanel.knowledgeReindexFailed"),
                    );
                  })
                  .finally(() => setActionBusy(false));
              }}
              className={`${btnSecondaryClass} disabled:opacity-50`}
            >
              {t("configPanel.reindex")}
            </button>
          ) : null}
          <button
            type="button"
            disabled={uploading}
            onClick={() => inputRef.current?.click()}
            className="cursor-pointer rounded-lg bg-slate-950 px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300 disabled:opacity-50"
          >
            {uploading ? t("configPanel.uploading") : t("configPanel.uploadAndVectorize")}
          </button>
        </div>
      </div>

      {error ? (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
          {error}
        </div>
      ) : null}

      {loading ? (
        <p className="text-sm text-slate-500">{t("configPanel.loading")}</p>
      ) : documents.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noKnowledgeDocuments")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.noKnowledgeDocumentsHelp")}</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border bg-slate-50 text-[11px] uppercase tracking-[0.06em] text-slate-500">
              <tr>
                <th className="px-4 py-2.5 font-semibold">{t("configPanel.knowledgeFilename")}</th>
                <th className="px-4 py-2.5 font-semibold">{t("configPanel.knowledgeMimeType")}</th>
                <th className="px-4 py-2.5 font-semibold">{t("configPanel.knowledgeStatus")}</th>
                <th className="px-4 py-2.5 font-semibold">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => {
                const rowBusy = rowBusyId === doc.id;
                return (
                  <tr key={doc.id} className="border-b border-border last:border-b-0">
                    <td className="px-4 py-3">
                      <div className="font-medium text-slate-900">{doc.filename}</div>
                      <div className="mt-0.5 font-mono text-[10px] text-slate-400">{doc.id}</div>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-600">{doc.mimeType || "—"}</td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${statusClass(doc.status)}`}>
                        {knowledgeDocStatusLabel(doc.status, t)}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-2">
                        {onRetryDocument && (doc.status === "failed" || doc.status === "ready") ? (
                          <button
                            type="button"
                            disabled={rowBusy}
                            onClick={() => void handleRetry(doc.id)}
                            className={`${btnSecondaryClass} disabled:opacity-50`}
                          >
                            {doc.status === "failed"
                              ? t("configPanel.knowledgeRetry")
                              : t("configPanel.reindex")}
                          </button>
                        ) : null}
                        {onDeleteDocument ? (
                          <button
                            type="button"
                            disabled={rowBusy}
                            onClick={() => void handleDelete(doc.id)}
                            className="cursor-pointer rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-semibold text-rose-700 transition-colors duration-200 hover:bg-rose-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-200 disabled:opacity-50"
                          >
                            {t("common.delete")}
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <input
        ref={inputRef}
        type="file"
        accept=".txt,.md,.csv,.tsv,.json,.yaml,.yml,.pdf,text/plain,text/markdown,text/csv,application/json,application/pdf"
        className="hidden"
        onChange={(event) => {
          void handleUpload(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </div>
  );
}
