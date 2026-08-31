import { describe, expect, it } from "vitest";
import type { DatasourceTypeDto } from "../../../lib/config-api";
import {
  buildDatasourceSettingsForType,
  getDatasourceIconSrc,
  groupDatasourceTypes,
  summarizeDatasourceConnection,
} from "../datasource-metadata";

const types: DatasourceTypeDto[] = [
  {
    name: "postgresql",
    label: "PostgreSQL",
    enabled: true,
    description: "PostgreSQL read-only datasource.",
    parameters: [
      { name: "host", label: "Host", type: "string", required: true },
      { name: "port", label: "Port", type: "number", required: true, default_value: 5432 },
      { name: "database", label: "Database", type: "string", required: true },
      { name: "password", label: "Password", type: "password", required: true },
    ],
  },
  {
    name: "mongodb",
    label: "MongoDB",
    enabled: true,
    description: "MongoDB collection mapping.",
    parameters: [
      { name: "uri", label: "URI", type: "password", required: true },
      { name: "database", label: "Database", type: "string", required: true },
      { name: "sampleSize", label: "Schema Sample Size", type: "number", required: false, default_value: 20 },
    ],
  },
  {
    name: "unknown-disabled",
    label: "Unknown",
    enabled: false,
    parameters: [],
  },
];

describe("datasource metadata", () => {
  it("groups only enabled datasource types into product categories", () => {
    const groups = groupDatasourceTypes(types);

    expect(groups.flatMap((group) => group.types.map((type) => type.name))).toEqual([
      "postgresql",
      "mongodb",
    ]);
    expect(groups.find((group) => group.id === "relational")?.types[0]?.label).toBe("PostgreSQL");
    expect(groups.find((group) => group.id === "nosql-search")?.types[0]?.label).toBe("MongoDB");
  });

  it("builds type-specific default settings from backend parameters", () => {
    const settings = buildDatasourceSettingsForType(types[0]!);

    expect(settings).toMatchObject({
      datasourceId: "custom-postgresql",
      type: "postgresql",
      mode: "readonly",
      port: "5432",
      denyWrite: "true",
      allowSample: "true",
      maxSampleRows: "100",
    });
    expect(settings.password).toBe("");
  });

  it("returns icon paths for known datasource types", () => {
    expect(getDatasourceIconSrc("postgresql")).toBe("/assets/db-icons/postgresql.svg");
    expect(getDatasourceIconSrc("oceanbase")).toBeNull();
    expect(getDatasourceIconSrc(undefined)).toBeNull();
  });

  it("summarizes connection details by datasource type", () => {
    expect(
      summarizeDatasourceConnection({
        id: "sales-pg",
        name: "Sales PG",
        description: "",
        enabled: true,
        settings: {
          type: "postgresql",
          host: "127.0.0.1",
          port: "5432",
          database: "sales",
        },
      }),
    ).toBe("127.0.0.1:5432 · sales");

    expect(
      summarizeDatasourceConnection({
        id: "mongo",
        name: "Mongo",
        description: "",
        enabled: true,
        settings: { type: "mongodb", database: "events" },
      }),
    ).toBe("events");
  });
});
