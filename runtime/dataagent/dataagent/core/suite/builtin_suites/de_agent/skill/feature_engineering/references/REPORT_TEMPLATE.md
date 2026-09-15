# Feature Engineering Report Template

> **Usage**: The agent fills in this template section by section as execution proceeds. Each Phase appends its corresponding section. `{{placeholder}}` marks content to be replaced with actual values.

---

## Executive Summary

> Filled in during Phase 6, after all analysis is complete. Leave this section blank until then.

- **Data source**: {{INPUT_SOURCE}}
- **Original scale**: {{n_rows}} rows × {{n_cols}} columns
- **Sampling applied**: {{Yes / No — if yes, describe sampling ratio and method}}
- **Final feature count**: {{original_count}} → {{final_count}} ({{added}} added, {{removed}} removed)
- **Data quality rating**: {{Good / Fair / Poor}}
- **Key findings**:
  1. {{finding_1}}
  2. {{finding_2}}
  3. {{finding_3}}

---

## 1. Data Overview

### 1.1 Basic Information

| Metric | Value |
|--------|-------|
| Row count | {{n_rows}} |
| Column count | {{n_cols}} |
| Numeric columns | {{n_numeric}} |
| Categorical columns | {{n_categorical}} |
| Time column | {{column name, or "None"}} |
| Multi-table input | {{Yes / No — if yes, list table names}} |

### 1.2 Sample Data (First 5 Rows)

```
{{df.head().to_markdown() output}}
```

### 1.3 Numeric Column Summary Statistics

```
{{df.describe().to_markdown() output}}
```

---

## 2. Data Quality Assessment

**Quality rating**: {{Good / Fair / Poor}}

### 2.1 Column-Level Quality Report

| Column | Data Type | Missing Rate | Outlier Rate | Issues |
|--------|-----------|--------------|--------------|--------|
| {{col_1}} | {{dtype}} | {{x.x%}} | {{x.x%}} | {{None / high missing / mixed types / ...}} |

### 2.2 Duplicate Rows

- Duplicate row count: {{dup_count}} ({{dup_rate%}} of total)
- Action taken: {{dropped / retained — explain reason}}

### 2.3 Quality Issues and Revised Plan

{{If rated "Poor": explain the adjusted feature engineering route here. If "Good" or "Fair": briefly describe the handling strategy.}}

---

## 3. Feature Construction Process

> Recorded sub-task by sub-task as Phase 4 executes. Each entry includes: intent, code block, and observation.

### 3.1 Data Cleaning and Encoding *(SKILL.md Phase 4.1)*

**[Intent]** {{Describe what problem this step solves and why this strategy was chosen}}

```python
# {{Comment: what this code block does}}
{{actual executed code}}
```

**[Observation]** {{Key result after execution — e.g., "Missing values reduced to 0; encoding added 8 new columns"}}

---

### 3.2 Numeric Feature Transformation *(SKILL.md Phase 4.2)*

**[Intent]** {{Describe which skewed columns were detected and which transformation was applied}}

```python
{{actual executed code}}
```

**[Observation]** {{Result}}

---

### 3.3 Feature Combination *(SKILL.md Phase 4.3)*

**[Intent]** {{Describe which combination features were constructed and the rationale (business logic / correlation analysis)}}

```python
{{actual executed code}}
```

**[Observation]** {{Result}}

---

### 3.4 Time-Series Feature Construction *(SKILL.md Phase 4.4 — if triggered)*

**[Trigger]** {{Time column detected: {{col_name}}, data frequency: {{daily / hourly / monthly}}}}

**[Intent]** {{Describe which time-series features were constructed}}

```python
{{actual executed code}}
```

**[Observation]** {{Result — note how NaN in rolling features was handled}}

---

### 3.5 Multi-Table Join Features *(SKILL.md Phase 4.5 — if triggered)*

**[Trigger]** {{Multi-table input detected: {{table names}}, join key: {{join_key}}}}

**[Intent]** {{Describe which cross-table aggregation features were constructed}}

```python
{{actual executed code}}
```

**[Observation]** {{Result — note whether temporal leakage prevention was applied}}

---

### 3.6 Automatic Feature Mining *(SKILL.md Phase 4.6)*

**[Intent]** {{Describe whether mutual information or variance ranking was used, and which high-value combinations were found}}

```python
{{actual executed code}}
```

**[Observation]** {{Result — list retained combination features and their correlation improvement}}

---

## 4. Feature Evaluation and Selection

### 4.1 Filtering Summary

| Filtering Step | Features Removed | Examples |
|----------------|-----------------|---------|
| Correlation filter (Pearson > 0.95) | {{N}} | {{feature_a, feature_b, ...}} |
| Variance filter (variance < 0.01) | {{N}} | {{feature_c, ...}} |
| **Features after filtering** | — | **{{N}}** |

### 4.2 Feature Importance (Top 20)

![Feature Importance](figures/feature_importance.png)

> {{If no TARGET_COL, replace with a variance ranking chart or remove this section}}

### 4.3 Feature Correlation Heatmap

![Correlation Heatmap](figures/correlation_heatmap.png)

### 4.4 Final Feature List

| Feature Name | Type | Source | Importance Score | Retention Reason |
|--------------|------|--------|-----------------|-----------------|
| {{feature_1}} | numeric / categorical | original / constructed | {{0.xx}} | {{reason}} |

### 4.5 Dimensionality Reduction Recommendation (if feature count > 100)

- Current feature count: {{N}}
- PCA components needed for 95% explained variance: {{K}}
- Recommendation: {{Whether to apply dimensionality reduction and why}}

---

## 5. Feature Distribution

![Feature Distributions](figures/distributions.png)

> Distribution histograms for all numeric features. Skewed distributions addressed in Phase 4.2.

---

## 6. Reusable Code Index

The complete feature engineering pipeline code is at: `code/feature_pipeline.py`

**Function interface**:

```python
from feature_pipeline import run_feature_pipeline

# df: original DataFrame (same schema as used in this analysis)
# target_col: optional; target column name (affects encoding and evaluation strategy)
# Returns: processed DataFrame containing all final feature columns
features_df = run_feature_pipeline(df, target_col='{{TARGET_COL or None}}')
```

**Runtime dependencies**:

```
pandas >= 1.3.0
numpy >= 1.20.0
scikit-learn >= 0.24.0
matplotlib >= 3.3.0
seaborn >= 0.11.0
scipy >= 1.6.0         # Box-Cox / Yeo-Johnson
statsmodels >= 0.12.0  # Seasonal decomposition (required for time-series features)
```

**Deployment note**: `feature_pipeline.py` was generated from exploratory analysis on sampled data. Before deploying to production clusters:
- Re-fit all encoders and scalers on the full dataset
- For target encoding: fit on training set only to prevent test set leakage
- Adjust rolling window parameters for production data volume if needed

