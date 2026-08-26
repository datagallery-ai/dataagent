---
name: data_analysis_report
description: Use for structured data-analysis deliverables that read dataset files and produce `report.md` with evidence mapping, metric validation, figure interpretation, or final self-check. Do not use for conceptual Q&A with no dataset or artifact.
---

# Data Analysis Report Skill

Use this skill when producing `report.md` for evaluation-style data analysis tasks where scoring emphasizes completeness, accuracy, insightfulness, and visualization. *Reference example:* DACOMP uses a similar rubric; adapt wording to your actual task.

## Core Rule

Scoring is based on `report.md` content. Do not rely on external files as implicit evidence. Every key claim must be explicitly written in the report with numbers and evidence anchors.

## Required Workflow

1. Build a requirement list from the task statement and rubric language.
2. Create a **Requirement-Evidence Matrix** and place it near the top of `report.md`.
3. For each core metric, output a **Metric Card** with formula, fields, filters, and validation evidence.
4. Add cross-checks and edge-case checks before writing final conclusions.
5. Write actionable recommendations with trigger thresholds and rollback criteria.
6. Ensure every figure is interpreted in text, not only displayed.
7. End with a pass/fail self-check table.

## Requirement-Evidence Matrix (mandatory)

Columns:
- Requirement ID
- Original requirement
- Implemented method
- Evidence anchor (table/figure/section)
- Status (`Done` / `Partial` / `Not Done`)
- Gap impact and risk level

Do not mark `Done` without an evidence anchor.

## Metric Card Template (mandatory for each core metric)

- Definition: formula, numerator/denominator, time window, filters, grouping.
- Field mapping: source fields to analytic fields and units.
- Calculation result: sample size and key statistics.
- Cross-check:
  - Method A vs Method B (e.g., SQL vs Python) with delta.
  - Two manual recomputation samples (input -> intermediate -> output).
- Edge checks: denominator=0, missing values, outliers, unit conversion.
- Business interpretation: what decision this metric supports.

## Insight and Action Cards

At least 5 insight cards:
- Finding
- Numeric evidence
- Business implication

At least 3 action cards:
- Target segment
- Trigger condition (quantified threshold)
- Action
- Expected benefit range
- Monitoring KPI
- Failure risk
- Rollback condition

## Figure Rules

For each figure, include:
- Complete title, axis labels, unit, legend, sample note.
- Optional baseline/threshold line if relevant.
- A 3-line caption directly below the figure:
  - `Figure X Conclusion:`
  - `Figure X Evidence:`
  - `Figure X Action:`

The main text must explicitly reference figures and explain trends, anomalies, or segment differences.

## report.md Minimal Structure

1. Executive Summary
2. Requirement-Evidence Matrix
3. Data Scope and Metric Definitions
4. Core Metric Cards and Validation
5. Sensitivity / Counterfactual Checks
6. Insights and Action Cards
7. Visualization Interpretation
8. Risks and Limitations
9. Final Self-Check (pass/fail + evidence anchor)

## Final Gate

Before finishing, verify:
- All requirements are mapped in the matrix.
- Every core conclusion has numeric evidence.
- Cross-check and manual recomputation are present.
- Sensitivity and counterfactual checks are present.
- Recommendations are executable and quantified.
- Every figure is explained in text with actionable takeaway.
