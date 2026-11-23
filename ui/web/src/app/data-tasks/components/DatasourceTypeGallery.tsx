"use client";

import { useMemo, useState } from "react";
import type { DatasourceTypeDto } from "../../../lib/config-api";
import { useT } from "../../../i18n/locale-context";
import {
  filterDatasourceTypeGroups,
  groupDatasourceTypes,
  localizeDatasourceTypeGroups,
} from "../datasource-metadata";
import { DatasourceTypeIcon } from "./DatasourceTypeIcon";

type DatasourceTypeGalleryProps = {
  types: DatasourceTypeDto[];
  onSelect: (type: DatasourceTypeDto) => void;
};

export function DatasourceTypeGallery({ types, onSelect }: DatasourceTypeGalleryProps) {
  const t = useT();
  const [query, setQuery] = useState("");
  const groups = useMemo(
    () => localizeDatasourceTypeGroups(groupDatasourceTypes(types), t),
    [t, types],
  );
  const filteredGroups = useMemo(
    () => filterDatasourceTypeGroups(groups, query),
    [groups, query],
  );

  return (
    <section className="space-y-4">
      <div className="rounded-2xl border border-border bg-surface-subtle p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-slate-950">{t("gallery.title")}</h3>
            <p className="mt-1 max-w-2xl text-xs leading-5 text-slate-600">
              {t("gallery.help")}
            </p>
          </div>
          <label className="min-w-[220px] flex-1 sm:max-w-xs">
            <span className="sr-only">{t("gallery.searchAria")}</span>
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("gallery.searchPlaceholder")}
              className="h-9 w-full rounded-lg border border-border bg-white px-3 text-sm text-slate-900 outline-none transition-colors duration-200 placeholder:text-slate-400 focus:border-primary-light"
            />
          </label>
        </div>
      </div>

      {filteredGroups.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center text-sm text-slate-500">
          {t("gallery.empty")}
        </div>
      ) : (
        filteredGroups.map((group) => (
          <section key={group.id} className="space-y-2">
            <div>
              <h4 className="text-xs font-semibold uppercase tracking-[0.08em] text-slate-500">
                {group.title}
              </h4>
              <p className="mt-0.5 text-xs text-slate-400">{group.description}</p>
            </div>
            <div className="grid gap-2 [grid-template-columns:repeat(auto-fill,minmax(min(100%,240px),240px))]">
              {group.types.map((type) => {
                return (
                  <button
                    key={type.name}
                    type="button"
                    onClick={() => onSelect(type)}
                    className="group flex min-h-[104px] cursor-pointer items-start gap-3 rounded-xl border border-border bg-white p-3 text-left transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
                  >
                    <DatasourceTypeIcon
                      typeName={type.name}
                      className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold text-slate-950">
                        {type.label}
                      </span>
                      <span className="mt-0.5 block font-mono text-[10px] text-slate-400">
                        {type.name}
                      </span>
                      <span className="mt-1.5 line-clamp-2 block text-xs leading-5 text-slate-500">
                        {type.description || t("gallery.defaultDescription")}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>
          </section>
        ))
      )}
    </section>
  );
}
