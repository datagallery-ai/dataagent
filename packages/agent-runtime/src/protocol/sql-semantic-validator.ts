import sqlParser from "node-sql-parser";

import type {
  AnalysisAssertion,
  AnalysisScalar,
  AnalysisValidationFinding,
  SqlSemanticConstraint
} from "./analysis-contract.js";

type SqlParseResult = {
  tableList: string[];
  columnList: string[];
  ast: unknown;
};

type SqlPredicate = {
  column: string;
  operator: string;
  value: AnalysisScalar;
};

type SqlAggregate = {
  name: string;
  column?: string;
  alias?: string;
};

const parser = new sqlParser.Parser();

/** Compare parsed SQL semantics with the selected authoritative analysis assertions. */
export const validateSqlSemantics = (
  sql: string,
  dialect: string | undefined,
  assertions: AnalysisAssertion[]
): AnalysisValidationFinding[] => {
  let parsed: SqlParseResult;
  try {
    parsed = parser.parse(sql, { database: parserDialect(dialect) }) as SqlParseResult;
  } catch (error) {
    return [finding(
      "SQL_SEMANTIC_PARSE_FAILED",
      `SQL semantic parsing failed: ${error instanceof Error ? error.message : String(error)}`
    )];
  }
  if (Array.isArray(parsed.ast)) {
    return [finding("SQL_SEMANTIC_MULTIPLE_STATEMENTS", "Exactly one SELECT statement is required.")];
  }
  const ast = asRecord(parsed.ast);
  if (ast.type !== "select") {
    return [finding("SQL_SEMANTIC_SELECT_REQUIRED", "SQL semantic validation requires a SELECT statement.")];
  }
  const tables = parsed.tableList.map((entry) => normalizeIdentifier(entry.split("::").at(-1) ?? entry));
  const columns = parsed.columnList.map((entry) => normalizeIdentifier(entry.split("::").at(-1) ?? entry));
  const aggregates = collectAggregates(ast.columns);
  const groupBy = collectGroupBy(ast.groupby);
  const predicates = collectPredicates(ast.where);
  return assertions.flatMap((assertion) => {
    if (assertion.kind === "manual") return [];
    const constraints: SqlSemanticConstraint[] = [
      ...assertion.sourceTables.map((table) => ({ kind: "source" as const, table })),
      ...assertion.sqlConstraints
    ];
    return constraints.flatMap((constraint) => validateConstraint(
      constraint,
      { aggregates, columns, groupBy, predicates, tables },
      assertion.id
    ));
  });
};

const validateConstraint = (
  constraint: SqlSemanticConstraint,
  sql: {
    aggregates: SqlAggregate[];
    columns: string[];
    groupBy: string[];
    predicates: SqlPredicate[];
    tables: string[];
  },
  assertionId: string
): AnalysisValidationFinding[] => {
  if (constraint.kind === "source") {
    return sql.tables.includes(normalizeIdentifier(constraint.table))
      ? []
      : [finding(`SQL_SEMANTIC_SOURCE_MISSING:${constraint.table}`, `Required source ${constraint.table} is missing.`, assertionId)];
  }
  if (constraint.kind === "column") {
    return sql.columns.includes(normalizeIdentifier(constraint.column))
      ? []
      : [finding(`SQL_SEMANTIC_COLUMN_MISSING:${constraint.column}`, `Required column ${constraint.column} is missing.`, assertionId)];
  }
  if (constraint.kind === "aggregate") {
    const matches = sql.aggregates.some((aggregate) =>
      aggregate.name === constraint.function.toLowerCase()
      && (!constraint.column || aggregate.column === normalizeIdentifier(constraint.column))
      && (!constraint.alias || aggregate.alias === normalizeIdentifier(constraint.alias)));
    return matches ? [] : [finding(
      `SQL_SEMANTIC_AGGREGATE_MISSING:${constraint.function}`,
      `Expected ${formatAggregate({
        name: constraint.function,
        ...(constraint.column ? { column: normalizeIdentifier(constraint.column) } : {}),
        ...(constraint.alias ? { alias: normalizeIdentifier(constraint.alias) } : {})
      })}, but observed ${formatObservedAggregates(sql.aggregates)}.`,
      assertionId
    )];
  }
  if (constraint.kind === "group_by") {
    const missing = constraint.columns.filter((column) => !sql.groupBy.includes(normalizeIdentifier(column)));
    return missing.map((column) => finding(
      `SQL_SEMANTIC_GROUP_BY_MISSING:${column}`,
      `Required group-by column ${column} is missing.`,
      assertionId
    ));
  }
  if (constraint.kind === "filter") {
    return predicateExists(sql.predicates, constraint.column, operatorToken(constraint.operator), constraint.value)
      ? []
      : [finding(
          `SQL_SEMANTIC_FILTER_MISSING:${constraint.column}:${constraint.operator}`,
          `Required filter ${constraint.column} ${constraint.operator} is missing.`,
          assertionId
        )];
  }
  const end = constraint.endInclusive ? nextDate(constraint.end) : constraint.end;
  const hasStart = predicateExists(sql.predicates, constraint.column, ">=", constraint.start);
  const hasEnd = predicateExists(sql.predicates, constraint.column, "<", end);
  return [
    ...(hasStart ? [] : [finding(
      `SQL_SEMANTIC_TIME_START_MISSING:${constraint.column}`,
      `Required start boundary ${constraint.start} is missing.`,
      assertionId
    )]),
    ...(hasEnd ? [] : [finding(
      `SQL_SEMANTIC_TIME_END_MISSING:${constraint.column}`,
      `Required half-open end boundary ${end} is missing.`,
      assertionId
    )])
  ];
};

const collectAggregates = (value: unknown): SqlAggregate[] => {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const record = asRecord(item);
    const expression = asRecord(record.expr);
    if (expression.type !== "aggr_func" || typeof expression.name !== "string") return [];
    const argument = asRecord(asRecord(expression.args).expr);
    const column = argument.type === "column_ref" && typeof argument.column === "string"
      ? normalizeIdentifier(argument.column)
      : argument.type === "star" && argument.value === "*"
        ? "*"
        : undefined;
    return [{
      name: expression.name.toLowerCase(),
      ...(column ? { column } : {}),
      ...(typeof record.as === "string" ? { alias: normalizeIdentifier(record.as) } : {})
    }];
  });
};

const formatAggregate = (aggregate: SqlAggregate): string => {
  const argument = aggregate.column === undefined ? "" : `(${aggregate.column})`;
  const alias = aggregate.alias === undefined ? "" : ` AS ${aggregate.alias}`;
  return `${aggregate.name.toUpperCase()}${argument}${alias}`;
};

const formatObservedAggregates = (aggregates: SqlAggregate[]): string =>
  aggregates.length === 0 ? "no aggregate expressions" : aggregates.map(formatAggregate).join(", ");

const collectGroupBy = (value: unknown): string[] => {
  const columns = asRecord(value).columns;
  if (!Array.isArray(columns)) return [];
  return columns.flatMap((column) => {
    const record = asRecord(column);
    return record.type === "column_ref" && typeof record.column === "string"
      ? [normalizeIdentifier(record.column)]
      : [];
  });
};

const collectPredicates = (value: unknown): SqlPredicate[] => {
  const record = asRecord(value);
  if (record.type !== "binary_expr" || typeof record.operator !== "string") return [];
  if (record.operator.toUpperCase() === "AND") {
    return [...collectPredicates(record.left), ...collectPredicates(record.right)];
  }
  const left = asRecord(record.left);
  const right = asRecord(record.right);
  if (left.type !== "column_ref" || typeof left.column !== "string") return [];
  const scalar = sqlLiteral(right);
  return scalar === undefined ? [] : [{
    column: normalizeIdentifier(left.column),
    operator: record.operator,
    value: scalar
  }];
};

const sqlLiteral = (value: Record<string, unknown>): AnalysisScalar | undefined => {
  if (["single_quote_string", "double_quote_string", "string"].includes(String(value.type))) {
    return typeof value.value === "string" ? value.value : undefined;
  }
  if (value.type === "number") return typeof value.value === "number" ? value.value : Number(value.value);
  if (value.type === "bool") return Boolean(value.value);
  if (value.type === "null") return null;
  return undefined;
};

const predicateExists = (
  predicates: SqlPredicate[],
  column: string,
  operator: string,
  value: AnalysisScalar
): boolean => predicates.some((predicate) => predicate.column === normalizeIdentifier(column)
  && predicate.operator === operator && predicate.value === value);

const operatorToken = (operator: "eq" | "gt" | "gte" | "lt" | "lte"): string => ({
  eq: "=",
  gt: ">",
  gte: ">=",
  lt: "<",
  lte: "<="
})[operator];

const nextDate = (value: string): string => {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/u.exec(value);
  if (!matched) return value;
  const date = new Date(Date.UTC(Number(matched[1]), Number(matched[2]) - 1, Number(matched[3])));
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString().slice(0, 10);
};

const parserDialect = (dialect: string | undefined): string => {
  const normalized = dialect?.toLowerCase();
  if (normalized === "postgres" || normalized === "postgresql") return "Postgresql";
  if (normalized === "mysql") return "MySQL";
  return "SQLite";
};

const normalizeIdentifier = (value: string): string => value.replace(/^[`"[]|[`"\]]$/gu, "").toLowerCase();
const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
const finding = (code: string, message: string, assertionId?: string): AnalysisValidationFinding => ({
  code,
  message,
  severity: "error",
  ...(assertionId ? { assertionId } : {})
});
