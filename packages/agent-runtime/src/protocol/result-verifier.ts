import type {
  AnalysisAssertion,
  AnalysisScalar,
  AnalysisValidationFinding,
  AnalysisValueOperand,
  AnalysisVerifiedValue
} from "./analysis-contract.js";

export type AnalysisTabularResult = {
  columns: string[];
  rows: unknown[];
  rowCount: number;
};

export type AnalysisResultVerification = {
  valid: boolean;
  findings: AnalysisValidationFinding[];
  verifiedValues: AnalysisVerifiedValue[];
};

/** Verify deterministic result invariants and extract claimable values. */
export const verifyAnalysisResult = (
  result: AnalysisTabularResult,
  assertions: AnalysisAssertion[]
): AnalysisResultVerification => {
  const rows = normalizeRows(result);
  const findings: AnalysisValidationFinding[] = [];
  for (const assertion of assertions) {
    for (const check of assertion.resultChecks) {
      if (check.kind === "non_empty") {
        if (rows.length === 0) {
          findings.push({
            code: "RESULT_CHECK_NON_EMPTY_FAILED",
            message: `Non-empty check failed for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "row_count") {
        const passed = (check.min === undefined || result.rowCount >= check.min)
          && (check.max === undefined || result.rowCount <= check.max);
        if (!passed) {
          findings.push({
            code: "RESULT_CHECK_ROW_COUNT_FAILED",
            message: `Row count ${result.rowCount} is outside the declared range for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "not_null") {
        for (const field of check.fields) {
          if (rows.some((row) => !Object.hasOwn(row, field) || row[field] === null)) {
            findings.push({
              code: `RESULT_CHECK_NOT_NULL_FAILED:${field}`,
              message: `Field ${field} contains missing or null values for assertion ${assertion.id}.`,
              severity: check.required ? "error" : "warning",
              assertionId: assertion.id
            });
          }
        }
        continue;
      }
      if (check.kind === "unique") {
        const keys = rows.map((row) => JSON.stringify(check.fields.map((field) => row[field])));
        if (new Set(keys).size !== keys.length) {
          findings.push({
            code: `RESULT_CHECK_UNIQUE_FAILED:${check.fields.join(",")}`,
            message: `Result grain is not unique for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "equals") {
        const left = resolveOperand(check.left, rows);
        const right = resolveOperand(check.right, rows);
        if (!valuesEqual(left, right, check.tolerance ?? 0)) {
          findings.push({
            code: "RESULT_CHECK_EQUALS_FAILED",
            message: `Equality check failed for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "sum") {
        const total = resolveOperand(check.total, rows);
        const parts = check.parts.map((part) => resolveOperand(part, rows));
        const partSum = parts.every((part) => typeof part === "number")
          ? parts.reduce<number>((sum, part) => sum + (part as number), 0)
          : undefined;
        if (typeof total !== "number" || partSum === undefined
          || Math.abs(total - partSum) > (check.tolerance ?? 0)) {
          findings.push({
            code: "RESULT_CHECK_SUM_FAILED",
            message: `Sum reconciliation failed for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "comparison") {
        const left = resolveOperand(check.left, rows);
        const right = resolveOperand(check.right, rows);
        if (!compareValues(left, right, check.operator ?? "eq", check.tolerance ?? 0)) {
          findings.push({
            code: `RESULT_CHECK_COMPARISON_FAILED:${check.operator}`,
            message: `Comparison ${check.operator} failed for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind === "budget_conservation") {
        const budget = resolveOperand(check.left, rows);
        const conserved = resolveOperand(check.right, rows);
        if (!valuesEqual(budget, conserved, check.tolerance ?? 0)) {
          findings.push({
            code: "RESULT_CHECK_BUDGET_CONSERVATION_FAILED",
            message: `Budget conservation failed for assertion ${assertion.id}.`,
            severity: check.required ? "error" : "warning",
            assertionId: assertion.id
          });
        }
        continue;
      }
      if (check.kind !== "ratio") continue;
      const value = resolveOperand(check.value, rows);
      const numerator = resolveOperand(check.numerator, rows);
      const denominator = resolveOperand(check.denominator, rows);
      const scale = check.scale ?? 1;
      const tolerance = check.tolerance ?? 0.000001;
      const passed = typeof value === "number" && typeof numerator === "number"
        && typeof denominator === "number" && denominator !== 0
        && Math.abs(value - numerator / denominator * scale) <= tolerance;
      if (!passed) {
        findings.push({
          code: "RESULT_CHECK_RATIO_FAILED",
          message: `Ratio check failed for assertion ${assertion.id}.`,
          severity: check.required ? "error" : "warning",
          assertionId: assertion.id
        });
      }
    }
  }
  const valid = !findings.some((finding) => finding.severity === "error");
  const verifiedValues = valid ? assertions.flatMap((assertion) => assertion.claimValues.flatMap((spec) => {
    const value = resolveOperand({
      field: spec.field,
      ...(spec.selector ? { selector: spec.selector } : {})
    }, rows);
    if (value === undefined) {
      if (spec.required) {
        findings.push({
          code: `RESULT_CLAIM_VALUE_MISSING:${spec.name}`,
          message: `Claim value ${spec.name} is missing for assertion ${assertion.id}.`,
          severity: "error",
          assertionId: assertion.id
        });
      }
      return [];
    }
    return [{
      name: spec.name,
      value,
      ...(spec.unit ? { unit: spec.unit } : {}),
      tolerance: spec.tolerance ?? 0,
      assertionId: assertion.id
    }];
  })) : [];
  return {
    valid: valid && !findings.some((finding) => finding.severity === "error"),
    findings,
    verifiedValues: findings.some((finding) => finding.severity === "error") ? [] : verifiedValues
  };
};

const normalizeRows = (result: AnalysisTabularResult): Array<Record<string, AnalysisScalar>> => result.rows.map((row) => {
  if (isRecord(row)) {
    return Object.fromEntries(Object.entries(row).map(([key, value]) => [key, asScalar(value)]));
  }
  if (!Array.isArray(row)) return {};
  return Object.fromEntries(result.columns.map((column, index) => [column, asScalar(row[index])]));
});

const resolveOperand = (
  operand: AnalysisValueOperand,
  rows: Array<Record<string, AnalysisScalar>>
): AnalysisScalar | undefined => {
  if (Object.hasOwn(operand, "literal")) return operand.literal;
  if (!operand.field) return undefined;
  const matchingRows = operand.selector
    ? rows.filter((row) => Object.entries(operand.selector ?? {}).every(([field, value]) => row[field] === value))
    : rows;
  if (matchingRows.length !== 1) return undefined;
  return Object.hasOwn(matchingRows[0] as object, operand.field)
    ? matchingRows[0]?.[operand.field]
    : undefined;
};

const asScalar = (value: unknown): AnalysisScalar =>
  typeof value === "string" || typeof value === "number" || typeof value === "boolean" || value === null
    ? value
    : String(value ?? "");

const valuesEqual = (
  left: AnalysisScalar | undefined,
  right: AnalysisScalar | undefined,
  tolerance: number
): boolean => typeof left === "number" && typeof right === "number"
  ? Math.abs(left - right) <= tolerance
  : left !== undefined && right !== undefined && left === right;

const compareValues = (
  left: AnalysisScalar | undefined,
  right: AnalysisScalar | undefined,
  operator: "eq" | "gt" | "gte" | "lt" | "lte",
  tolerance: number
): boolean => {
  if (operator === "eq") return valuesEqual(left, right, tolerance);
  if (typeof left !== "number" || typeof right !== "number") return false;
  if (operator === "gt") return left > right + tolerance;
  if (operator === "gte") return left >= right - tolerance;
  if (operator === "lt") return left < right - tolerance;
  return left <= right + tolerance;
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
