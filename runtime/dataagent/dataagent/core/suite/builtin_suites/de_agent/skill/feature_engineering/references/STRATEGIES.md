# Feature Engineering Strategy Reference

This reference is consulted by the `feature_engineering` skill during execution. Each section provides decision rules and code examples for a specific feature type.

---

## 1. Numeric Features

### 1.1 When to Normalize / Standardize

| Scenario | Recommended Method | Reason |
|----------|--------------------|--------|
| Tree models (RF / XGBoost / LightGBM) | Not needed | Tree models are scale-invariant |
| Linear models / SVM / KNN / neural networks | StandardScaler | Removes unit-scale differences |
| Data has significant outliers | RobustScaler | Uses median/IQR, robust to outliers |
| Need [0, 1] range (e.g., neural network output layer) | MinMaxScaler | Sensitive to outliers — handle them first |

```python
from sklearn.preprocessing import StandardScaler, RobustScaler, MinMaxScaler

scaler = StandardScaler()
df[numeric_cols] = scaler.fit_transform(df[numeric_cols])
```

### 1.2 Handling Non-Normal Distributions

```python
import numpy as np
from scipy import stats

# Detect skewness (|skewness| > 1 is considered significant)
skewed_cols = df[numeric_cols].apply(lambda x: abs(x.skew()) > 1)

# Log transform (column values must be > 0; log1p handles zeros)
df[col] = np.log1p(df[col])

# Box-Cox (column values must be > 0; shift only when necessary)
shift = max(0, -df[col].min() + 1e-6)  # 0 if already positive, small offset otherwise
df[col], _ = stats.boxcox(df[col] + shift)

# Yeo-Johnson (allows negative values — preferred over Box-Cox when negatives exist)
from sklearn.preprocessing import PowerTransformer
pt = PowerTransformer(method='yeo-johnson')
df[[col]] = pt.fit_transform(df[[col]])
```

### 1.3 Outlier Handling Strategy

| Outlier Rate | Recommended Strategy |
|--------------|----------------------|
| < 1% | Drop outlier rows (if sample size allows) |
| 1%–5% | Winsorize: clip to [p1, p99] |
| > 5% | Binning: discretize the column; outliers fall into boundary bins |

```python
# Winsorize
p1, p99 = df[col].quantile([0.01, 0.99])
df[col] = df[col].clip(lower=p1, upper=p99)

# IQR outlier detection
Q1, Q3 = df[col].quantile([0.25, 0.75])
IQR = Q3 - Q1
outlier_mask = (df[col] < Q1 - 1.5 * IQR) | (df[col] > Q3 + 1.5 * IQR)
outlier_rate = outlier_mask.mean()
```

---

## 2. Categorical Features

### 2.1 Encoding Decision Tree

```
Cardinality ≤ 10?
  ├── Yes → One-hot encoding (pd.get_dummies)
  └── No  → Target column <TARGET_COL> present?
              ├── Yes → Target encoding (replace with target mean; use k-fold to prevent leakage)
              └── No  → Frequency encoding (replace with frequency value)

Is the category ordered (e.g., Low / Medium / High, rating scale)?
  └── Yes → Ordinal encoding (manually map to ordered integers)
```

```python
# One-hot encoding
df = pd.get_dummies(df, columns=[col], drop_first=True)

# Frequency encoding
freq_map = df[col].value_counts(normalize=True).to_dict()
df[f'{col}_freq'] = df[col].map(freq_map)

# Target encoding (simple version — use k-fold in production to prevent leakage)
target_mean = df.groupby(col)[target_col].mean().to_dict()
df[f'{col}_target_enc'] = df[col].map(target_mean)

# Ordinal encoding
order_map = {'Low': 0, 'Medium': 1, 'High': 2}
df[col] = df[col].map(order_map)
```

### 2.2 High-Cardinality Category Handling

```python
# Merge rare categories (frequency < 1%) into 'Other'
value_counts = df[col].value_counts(normalize=True)
rare_cats = value_counts[value_counts < 0.01].index
df[col] = df[col].replace(rare_cats, 'Other')
```

---

## 3. Time-Series Features

### 3.1 Window Size Selection Reference

| Data Sampling Frequency | Recommended Windows | Captures |
|------------------------|---------------------|----------|
| Daily | 7, 30, 90 | Weekly / monthly / quarterly effects |
| Hourly | 24, 168 | Daily / weekly effects |
| Monthly | 3, 6, 12 | Quarterly / semi-annual / annual effects |

### 3.2 Seasonality Detection

```python
from statsmodels.tsa.seasonal import seasonal_decompose

# Time column must be set as index; data must have no missing values
ts = df.set_index(time_col)[value_col]
decomposition = seasonal_decompose(ts, model='additive', period=7)
seasonal_strength = decomposition.seasonal.std() / ts.std()
# seasonal_strength > 0.1 indicates significant seasonality
```

### 3.3 Lag Features and Rolling Statistics

```python
# Sort by time first (mandatory)
df = df.sort_values(time_col).reset_index(drop=True)

# Lag features
for lag in [1, 7, 30]:
    df[f'{value_col}_lag_{lag}'] = df[value_col].shift(lag)

# Rolling window statistics (min_periods=1 avoids excessive NaN at the start)
for window in [7, 30]:
    df[f'{value_col}_rolling_mean_{window}'] = (
        df[value_col].rolling(window, min_periods=1).mean()
    )
    df[f'{value_col}_rolling_std_{window}'] = (
        df[value_col].rolling(window, min_periods=1).std()
    )

# Timestamp decomposition
df['year'] = df[time_col].dt.year
df['month'] = df[time_col].dt.month
df['weekday'] = df[time_col].dt.weekday   # 0 = Monday, 6 = Sunday
df['hour'] = df[time_col].dt.hour
df['is_weekend'] = (df[time_col].dt.weekday >= 5).astype(int)
```

---

## 4. Multi-Table Features

### 4.1 Join Key Identification

```python
# Find shared columns between two tables
common_cols = set(df1.columns) & set(df2.columns)

# Verify whether a candidate key is unique in the secondary table (one-to-many join)
for key in common_cols:
    is_unique = df2[key].nunique() == len(df2)
    print(f"{key}: unique in df2 = {is_unique}")

# Verify left join does not lose primary table rows
# Note: only valid when df2 is unique on join_key (many-to-one from df1's perspective)
# For one-to-many joins, use the aggregation pattern in Section 4.2 instead
merged = df1.merge(df2, on=join_key, how='left')
assert len(merged) >= len(df1), "Primary table rows were lost — check join key"
```

### 4.2 Cross-Table Aggregation Features

```python
# Aggregate secondary table by join key
agg_df = df2.groupby(join_key).agg(
    value_count=(value_col, 'count'),
    value_sum=(value_col, 'sum'),
    value_mean=(value_col, 'mean'),
    value_max=(value_col, 'max'),
    value_min=(value_col, 'min'),
    value_std=(value_col, 'std'),
).reset_index()

# Merge into primary table
df1 = df1.merge(agg_df, on=join_key, how='left')
```

### 4.3 Temporal Join to Prevent Data Leakage

```python
# Both tables must be sorted by the time column
df1 = df1.sort_values(time_col)
df2 = df2.sort_values(time_col)

# merge_asof: for each primary table row, only join the most recent secondary
# table row whose timestamp is <= the primary row's timestamp
df_merged = pd.merge_asof(
    df1, df2,
    on=time_col,
    by=join_key,          # additional join key
    direction='backward'  # only use records at or before the current timestamp
)
```

---

## 5. Automatic Feature Mining

### 5.1 Decision Tree: Which Combination Features to Construct

```
Have <TARGET_COL>?
  ├── Yes → Compute mutual information (MI) for each feature
  │         Take Top-10 features by MI score
  │         Evaluate pairwise multiplicative/additive combinations among the top 5
  │         Keep combinations where correlation with target improves by > 10%
  └── No  → Rank features by variance; take Top-10 high-variance features
             Evaluate pairwise multiplicative combinations; keep Top-5 by variance

When to use cross features (categorical × categorical)?
  → Both categorical columns have cardinality ≤ 5: use string concatenation
  → Note: combined cardinality = cardinality_A × cardinality_B — avoid explosion

When to use aggregation features (groupby + agg)?
  → There is a clear group hierarchy (user → orders, product → category)
  → Fine-grained primary table rows need coarse-grained statistical context
```

### 5.2 Mutual Information Feature Mining Code

```python
from sklearn.feature_selection import mutual_info_regression, mutual_info_classif
from itertools import combinations
import pandas as pd

# Compute mutual information (classif for classification target, regression for continuous)
is_classification = df[target_col].nunique() <= 20  # heuristic: few unique values → classification
mi_fn = mutual_info_classif if is_classification else mutual_info_regression
mi = mi_fn(df[feature_cols].fillna(0), df[target_col])
mi_series = pd.Series(mi, index=feature_cols).sort_values(ascending=False)

top_features = mi_series.head(10).index.tolist()

# Construct multiplicative combinations and evaluate
original_corr = {f: abs(df[f].corr(df[target_col])) for f in top_features[:5]}

for f1, f2 in combinations(top_features[:5], 2):
    new_feat = df[f1] * df[f2]
    new_corr = abs(new_feat.corr(df[target_col]))
    baseline = max(original_corr[f1], original_corr[f2])
    if new_corr > baseline * 1.1:   # improvement > 10%
        df[f'{f1}_x_{f2}'] = new_feat
        print(f"Kept combination {f1}_x_{f2}: corr {baseline:.3f} → {new_corr:.3f}")
```
