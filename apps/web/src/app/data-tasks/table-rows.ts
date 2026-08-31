/** Resolve a cell value from a row object, including SQL `expr AS alias` column labels. */
export function resolveColumnValue(
  row: Record<string, unknown>,
  column: string,
): unknown {
  if (column in row) return row[column];

  const aliasMatch = /\bas\s+("?)([\w]+)\1\s*$/iu.exec(column.trim());
  if (aliasMatch?.[2] && aliasMatch[2] in row) {
    return row[aliasMatch[2]];
  }

  const bare = column.trim().replace(/^["'`]|["'`]$/gu, "");
  if (bare in row) return row[bare];

  return undefined;
}

/** Normalize API rows (array-of-arrays or array-of-objects) for table rendering. */
export function normalizeTableRows(
  columns: string[],
  rows: unknown[],
): unknown[][] {
  return rows.map((row) => {
    if (Array.isArray(row)) {
      return columns.map((_, index) => row[index] ?? null);
    }
    if (row && typeof row === "object") {
      const record = row as Record<string, unknown>;
      return columns.map((column) => resolveColumnValue(record, column) ?? null);
    }
    return columns.map(() => null);
  });
}

export function tableRowsLookEmpty(rows: unknown[][]): boolean {
  return rows.every((row) =>
    row.every((cell) => cell === null || cell === undefined || cell === ""),
  );
}

/** When SQL column labels don't match row keys, derive columns from the first object row. */
export function inferObjectRowColumns(rows: unknown[]): string[] {
  const firstObject = rows.find(
    (row) => row && typeof row === "object" && !Array.isArray(row),
  ) as Record<string, unknown> | undefined;
  return firstObject ? Object.keys(firstObject) : [];
}

export function normalizeSqlTable(
  columns: string[],
  rows: unknown[],
): { columns: string[]; rows: unknown[][] } {
  let normalized = normalizeTableRows(columns, rows);
  if (!tableRowsLookEmpty(normalized) || rows.length === 0) {
    return { columns, rows: normalized };
  }

  const inferredColumns = inferObjectRowColumns(rows);
  if (inferredColumns.length === 0) {
    return { columns, rows: normalized };
  }

  normalized = normalizeTableRows(inferredColumns, rows);
  return { columns: inferredColumns, rows: normalized };
}

export type TableSortState = {
  columnIndex: number;
  direction: "asc" | "desc";
};

function tableCellText(value: unknown): string {
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

export function filterTableRows(rows: unknown[][], query: string): unknown[][] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return rows;
  return rows.filter((row) =>
    row.some((cell) => tableCellText(cell).toLowerCase().includes(normalized)),
  );
}

export function sortTableRows(
  rows: unknown[][],
  sort: TableSortState | null,
): unknown[][] {
  if (!sort) return rows;
  const direction = sort.direction === "asc" ? 1 : -1;
  return [...rows].sort((left, right) => {
    const leftValue = left[sort.columnIndex];
    const rightValue = right[sort.columnIndex];
    const leftNumber =
      typeof leftValue === "number" ? leftValue : Number(tableCellText(leftValue));
    const rightNumber =
      typeof rightValue === "number" ? rightValue : Number(tableCellText(rightValue));

    if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
      return (leftNumber - rightNumber) * direction;
    }

    return tableCellText(leftValue).localeCompare(tableCellText(rightValue), "zh-Hans", {
      numeric: true,
      sensitivity: "base",
    }) * direction;
  });
}

function csvCell(value: unknown): string {
  const text = tableCellText(value);
  if (!/[",\r\n]/u.test(text)) return text;
  return `"${text.replace(/"/gu, '""')}"`;
}

export function tableToCsv(columns: string[], rows: unknown[][]): string {
  return [columns, ...rows].map((row) => row.map(csvCell).join(",")).join("\n");
}
