import type { DatasourceTypeDto, DatasourceTypeParamDto } from "../../lib/config-api";
import type { TranslateFn } from "../../i18n/types";
import { tx } from "../../i18n/config-labels";
import type { WorkspaceConfigItem } from "./data-task-state";

export type DatasourceTypeCategoryId =
  | "local-files"
  | "relational"
  | "warehouse"
  | "compatible-cn"
  | "query-engine"
  | "nosql-search"
  | "other";

export type DatasourceTypeCategory = {
  id: DatasourceTypeCategoryId;
  title: string;
  description: string;
  typeNames: string[];
};

export type DatasourceTypeGroup = DatasourceTypeCategory & {
  types: DatasourceTypeDto[];
};

export type DatasourceVisualMeta = {
  accentClass: string;
  mark: string;
};

const DATASOURCE_ICON_NAMES = new Set([
  "access",
  "bigquery",
  "clickhouse",
  "csv",
  "databricks",
  "doris",
  "duckdb",
  "elasticsearch",
  "gaussdb",
  "greenplum",
  "mariadb",
  "mongodb",
  "mysql",
  "opensearch",
  "oracle",
  "postgresql",
  "presto",
  "redis",
  "redshift",
  "snowflake",
  "spark",
  "sqlite",
  "sqlserver",
  "starrocks",
  "tidb",
  "trino",
  "xlsx",
]);

const DATASOURCE_ICON_BASE_PATH = "/assets/db-icons";

const DATASOURCE_TYPE_CATEGORIES: DatasourceTypeCategory[] = [
  {
    id: "local-files",
    title: "Local files",
    description: "Embedded, local, and uploaded tabular files.",
    typeNames: ["duckdb", "sqlite", "csv", "xlsx"],
  },
  {
    id: "relational",
    title: "Relational databases",
    description: "Classic OLTP and enterprise SQL databases.",
    typeNames: ["postgresql", "mysql", "sqlserver", "oracle"],
  },
  {
    id: "warehouse",
    title: "Warehouses and lakehouse",
    description: "Analytical stores and cloud SQL warehouses.",
    typeNames: ["clickhouse", "snowflake", "bigquery", "redshift", "databricks"],
  },
  {
    id: "compatible-cn",
    title: "Compatible and China-local engines",
    description: "PostgreSQL/MySQL-compatible analytical and domestic engines.",
    typeNames: ["gaussdb", "starrocks", "doris", "mariadb", "tidb", "oceanbase", "greenplum"],
  },
  {
    id: "query-engine",
    title: "Query engines",
    description: "Federated SQL and compute engines.",
    typeNames: ["trino", "presto", "spark"],
  },
  {
    id: "nosql-search",
    title: "NoSQL and search",
    description: "Collection, keyspace, and index-as-table mappings.",
    typeNames: ["mongodb", "redis", "elasticsearch", "opensearch"],
  },
  {
    id: "other",
    title: "Other sources",
    description: "Specialized adapters and file-backed sources.",
    typeNames: ["access"],
  },
];

const DATASOURCE_VISUAL_META: Record<string, DatasourceVisualMeta> = {
  duckdb: { mark: "D", accentClass: "bg-amber-50 text-amber-700 border-amber-200" },
  sqlite: { mark: "SQ", accentClass: "bg-sky-50 text-sky-700 border-sky-200" },
  csv: { mark: "CSV", accentClass: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  xlsx: { mark: "XLS", accentClass: "bg-green-50 text-green-700 border-green-200" },
  postgresql: { mark: "PG", accentClass: "bg-blue-50 text-blue-700 border-blue-200" },
  mysql: { mark: "MY", accentClass: "bg-orange-50 text-orange-700 border-orange-200" },
  clickhouse: { mark: "CH", accentClass: "bg-yellow-50 text-yellow-800 border-yellow-200" },
  snowflake: { mark: "SF", accentClass: "bg-cyan-50 text-cyan-700 border-cyan-200" },
  bigquery: { mark: "BQ", accentClass: "bg-indigo-50 text-indigo-700 border-indigo-200" },
  sqlserver: { mark: "MS", accentClass: "bg-red-50 text-red-700 border-red-200" },
  oracle: { mark: "OR", accentClass: "bg-rose-50 text-rose-700 border-rose-200" },
  mongodb: { mark: "M", accentClass: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  redis: { mark: "R", accentClass: "bg-red-50 text-red-700 border-red-200" },
  elasticsearch: { mark: "ES", accentClass: "bg-yellow-50 text-yellow-800 border-yellow-200" },
  opensearch: { mark: "OS", accentClass: "bg-teal-50 text-teal-700 border-teal-200" },
  databricks: { mark: "DB", accentClass: "bg-rose-50 text-rose-700 border-rose-200" },
  spark: { mark: "SP", accentClass: "bg-orange-50 text-orange-700 border-orange-200" },
  trino: { mark: "TR", accentClass: "bg-violet-50 text-violet-700 border-violet-200" },
  presto: { mark: "PR", accentClass: "bg-purple-50 text-purple-700 border-purple-200" },
  gaussdb: { mark: "G", accentClass: "bg-red-50 text-red-700 border-red-200" },
  starrocks: { mark: "SR", accentClass: "bg-blue-50 text-blue-700 border-blue-200" },
  doris: { mark: "DO", accentClass: "bg-slate-50 text-slate-700 border-slate-200" },
  mariadb: { mark: "MA", accentClass: "bg-orange-50 text-orange-700 border-orange-200" },
  tidb: { mark: "TI", accentClass: "bg-blue-50 text-blue-700 border-blue-200" },
  oceanbase: { mark: "OB", accentClass: "bg-cyan-50 text-cyan-700 border-cyan-200" },
  greenplum: { mark: "GP", accentClass: "bg-green-50 text-green-700 border-green-200" },
  redshift: { mark: "RS", accentClass: "bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200" },
  access: { mark: "AC", accentClass: "bg-pink-50 text-pink-700 border-pink-200" },
};

function parameterDefaultValue(parameter: DatasourceTypeParamDto): string {
  const value = parameter.default_value;
  if (value === undefined || value === null) return "";
  return String(value);
}

export function getDatasourceVisualMeta(typeName?: string): DatasourceVisualMeta {
  if (!typeName) {
    return { mark: "DB", accentClass: "bg-slate-50 text-slate-700 border-slate-200" };
  }
  return (
    DATASOURCE_VISUAL_META[typeName] ?? {
      mark: typeName.slice(0, 2).toUpperCase(),
      accentClass: "bg-slate-50 text-slate-700 border-slate-200",
    }
  );
}

export function getDatasourceIconSrc(typeName?: string): string | null {
  if (!typeName || !DATASOURCE_ICON_NAMES.has(typeName)) return null;
  return `${DATASOURCE_ICON_BASE_PATH}/${typeName}.svg`;
}

export function groupDatasourceTypes(types: DatasourceTypeDto[]): DatasourceTypeGroup[] {
  const enabled = types.filter((type) => type.enabled);
  const byName = new Map(enabled.map((type) => [type.name, type]));
  const used = new Set<string>();

  const groups = DATASOURCE_TYPE_CATEGORIES.flatMap((category) => {
    const groupedTypes = category.typeNames.flatMap((name) => {
      const type = byName.get(name);
      if (!type) return [];
      used.add(name);
      return [type];
    });
    return groupedTypes.length > 0 ? [{ ...category, types: groupedTypes }] : [];
  });

  const uncategorized = enabled.filter((type) => !used.has(type.name));
  if (uncategorized.length > 0) {
    groups.push({
      ...DATASOURCE_TYPE_CATEGORIES[DATASOURCE_TYPE_CATEGORIES.length - 1]!,
      typeNames: uncategorized.map((type) => type.name),
      types: uncategorized,
    });
  }

  return groups;
}

export function localizeDatasourceTypeGroups(
  groups: DatasourceTypeGroup[],
  t: TranslateFn,
): DatasourceTypeGroup[] {
  return groups.map((group) => ({
    ...group,
    title: tx(t, `datasourceCategories.${group.id}.title`, group.title),
    description: tx(
      t,
      `datasourceCategories.${group.id}.description`,
      group.description,
    ),
  }));
}

export function buildDatasourceSettingsForType(type: DatasourceTypeDto): Record<string, string> {
  const settings: Record<string, string> = {
    datasourceId: `custom-${type.name}`,
    type: type.name,
    mode: "readonly",
    denyWrite: "true",
    allowSample: "true",
    maxSampleRows: "100",
  };

  for (const parameter of type.parameters) {
    settings[parameter.name] =
      parameter.type === "password" ? "" : parameterDefaultValue(parameter);
  }

  return settings;
}

export function summarizeDatasourceConnection(item: WorkspaceConfigItem): string {
  const settings = item.settings ?? {};
  const type = settings.type ?? "";
  if (settings.host || settings.database) {
    const host = [settings.host, settings.port].filter(Boolean).join(":");
    return [host, settings.database].filter(Boolean).join(" · ");
  }
  if (type === "bigquery") {
    return [settings.projectId, settings.dataset].filter(Boolean).join(" · ");
  }
  if (type === "snowflake") {
    return [settings.account, settings.warehouse, settings.database].filter(Boolean).join(" · ");
  }
  if (settings.filePath) return settings.filePath;
  if (settings.path) return settings.path;
  if (settings.database) return settings.database;
  return item.description || item.id;
}

export function filterDatasourceTypeGroups(
  groups: DatasourceTypeGroup[],
  query: string,
): DatasourceTypeGroup[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return groups;
  return groups.flatMap((group) => {
    const types = group.types.filter((type) =>
      [type.name, type.label, type.description ?? "", group.title, group.description]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
    return types.length > 0 ? [{ ...group, types }] : [];
  });
}
