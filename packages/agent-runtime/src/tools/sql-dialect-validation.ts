export type SqlDialectIssue = {
  code: string;
  dialect: string;
  hint: string;
};

/** Detect common SQL constructs unsupported by the inspected datasource dialect. */
export const validateSqlDialect = (sql: string, dialect: string | undefined): SqlDialectIssue[] => {
  if (dialect?.toLowerCase() !== "sqlite") {
    return [];
  }
  const issues: SqlDialectIssue[] = [];
  if (/\bPERCENTILE_CONT\s*\([^)]*\)\s*WITHIN\s+GROUP\b/iu.test(sql)) {
    issues.push({
      code: "SQLITE_PERCENTILE_CONT_UNSUPPORTED",
      dialect: "sqlite",
      hint: "Use ROW_NUMBER and COUNT to select or interpolate ordered percentile rows."
    });
  }
  if (/\bILIKE\b/iu.test(sql)) {
    issues.push({
      code: "SQLITE_ILIKE_UNSUPPORTED",
      dialect: "sqlite",
      hint: "Use LIKE with COLLATE NOCASE for case-insensitive matching."
    });
  }
  if (/\bDATE_TRUNC\s*\(/iu.test(sql)) {
    issues.push({
      code: "SQLITE_DATE_TRUNC_UNSUPPORTED",
      dialect: "sqlite",
      hint: "Use strftime with the required date grain."
    });
  }
  if (/\bQUALIFY\b/iu.test(sql)) {
    issues.push({
      code: "SQLITE_QUALIFY_UNSUPPORTED",
      dialect: "sqlite",
      hint: "Move the window predicate into an outer SELECT WHERE clause."
    });
  }
  return issues;
};

/** Add stable dialect context and repair guidance to known backend syntax failures. */
export const enrichSqlDialectError = (error: unknown, dialect: string | undefined): Error => {
  const message = error instanceof Error ? error.message : String(error);
  if (dialect?.toLowerCase() !== "sqlite") {
    return error instanceof Error ? error : new Error(message);
  }
  if (/LIMIT clause should come after UNION(?: ALL)? not before/iu.test(message)) {
    return new Error(
      "SQL_DIALECT_ERROR:sqlite:SQLITE_UNION_LIMIT_POSITION:"
      + "Wrap the limited UNION branch in a subquery, or apply LIMIT after the complete UNION."
    );
  }
  if (/near ["']?WITHIN["']?: syntax error/iu.test(message)) {
    return new Error(
      "SQL_DIALECT_ERROR:sqlite:SQLITE_WITHIN_GROUP_UNSUPPORTED:"
      + "Replace WITHIN GROUP ordered-set aggregates with SQLite window functions."
    );
  }
  return error instanceof Error ? error : new Error(message);
};
