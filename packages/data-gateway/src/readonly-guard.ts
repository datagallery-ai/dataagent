export type SqlGuardResult =
  | {
      allowed: true;
      normalized_sql: string;
    }
  | {
      allowed: false;
      normalized_sql: string;
      reason: string;
    };

const DANGEROUS_SQL_KEYWORDS = [
  "INSERT",
  "UPDATE",
  "DELETE",
  "DROP",
  "ALTER",
  "TRUNCATE",
  "CREATE",
  "REPLACE",
  "MERGE",
  "CALL",
  "EXEC",
  "EXECUTE",
  "GRANT",
  "REVOKE",
  "COPY",
  "ATTACH",
  "DETACH",
  "PRAGMA",
  "VACUUM",
  "ANALYZE",
  "SET",
  "RESET",
  "LOAD"
];

export const stripSqlComments = (sql: string): string => {
  let result = "";
  let inSingleQuote = false;
  let inDoubleQuote = false;

  for (let index = 0; index < sql.length; index += 1) {
    const char = sql[index];
    const nextChar = sql[index + 1];

    if (inSingleQuote) {
      result += char;
      if (char === "'" && nextChar === "'") {
        result += nextChar;
        index += 1;
        continue;
      }
      if (char === "'") {
        inSingleQuote = false;
      }
      continue;
    }

    if (inDoubleQuote) {
      result += char;
      if (char === '"' && nextChar === '"') {
        result += nextChar;
        index += 1;
        continue;
      }
      if (char === '"') {
        inDoubleQuote = false;
      }
      continue;
    }

    if (char === "'") {
      inSingleQuote = true;
      result += char;
      continue;
    }

    if (char === '"') {
      inDoubleQuote = true;
      result += char;
      continue;
    }

    if (char === "-" && nextChar === "-") {
      index += 1;
      while (index + 1 < sql.length && sql[index + 1] !== "\n" && sql[index + 1] !== "\r") {
        index += 1;
      }
      result += " ";
      continue;
    }

    if (char === "/" && nextChar === "*") {
      index += 1;
      while (index + 1 < sql.length) {
        if (sql[index + 1] === "*" && sql[index + 2] === "/") {
          index += 2;
          break;
        }
        index += 1;
      }
      result += " ";
      continue;
    }

    result += char;
  }

  return result;
};

export const guardReadonlySql = (sql: string): SqlGuardResult => {
  const normalizedSql = normalizeSql(stripSqlComments(sql));

  if (!normalizedSql) {
    return { allowed: false, normalized_sql: normalizedSql, reason: "SQL is empty." };
  }

  if (hasMultipleStatements(normalizedSql)) {
    return { allowed: false, normalized_sql: normalizedSql, reason: "Multiple SQL statements are not allowed." };
  }

  const upperSql = stripQuotedSql(normalizedSql).toUpperCase();

  if (!upperSql.startsWith("SELECT ") && !upperSql.startsWith("WITH ")) {
    return { allowed: false, normalized_sql: normalizedSql, reason: "Only SELECT/WITH statements are allowed." };
  }

  const dangerousKeyword = DANGEROUS_SQL_KEYWORDS.find((keyword) => new RegExp(`\\b${keyword}\\b`, "u").test(upperSql));

  if (dangerousKeyword) {
    return { allowed: false, normalized_sql: normalizedSql, reason: `Dangerous keyword blocked: ${dangerousKeyword}.` };
  }

  return { allowed: true, normalized_sql: normalizedSql };
};

const normalizeSql = (sql: string): string => sql.trim().replace(/;+\s*$/u, "").replace(/\s+/gu, " ");

const hasMultipleStatements = (sql: string): boolean => {
  let inSingleQuote = false;
  let inDoubleQuote = false;

  for (let index = 0; index < sql.length; index += 1) {
    const char = sql[index];
    const nextChar = sql[index + 1];

    if (char === "'" && !inDoubleQuote) {
      if (inSingleQuote && nextChar === "'") {
        index += 1;
        continue;
      }

      inSingleQuote = !inSingleQuote;
      continue;
    }

    if (char === '"' && !inSingleQuote) {
      if (inDoubleQuote && nextChar === '"') {
        index += 1;
        continue;
      }

      inDoubleQuote = !inDoubleQuote;
      continue;
    }

    if (char === ";" && !inSingleQuote && !inDoubleQuote && sql.slice(index + 1).trim().length > 0) {
      return true;
    }
  }

  return false;
};

export const stripQuotedSql = (sql: string): string =>
  sql.replace(/'([^']|'')*'/gu, "''").replace(/"([^"]|"")*"/gu, '""');
