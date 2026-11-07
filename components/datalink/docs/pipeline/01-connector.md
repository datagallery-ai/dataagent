# Connector 模块实现文档

**源码路径：** `src/datalink/connector/`  
**输入：** `DatasourceConfig`  
**输出：** `DatasourceInfo`（tables + sample_data）

Connector 是流水线的第一步，负责从外部数据源读取 schema 元数据并采样行数据，供后续 Profiling 与推断使用。

---

## 1. 接口契约

`BaseConnector` 定义四个生命周期方法：

| 方法 | 职责 |
|------|------|
| `connect()` | 建立连接 / 加载文件到内存 |
| `get_datasource_info()` | 返回完整 `DatasourceInfo` |
| `get_sample_data(table, n)` | 按表名取 n 行样本 |
| `disconnect()` | 释放连接 / 清空缓存 |

Pipeline 通过 `_connect_datasource()` 调用：`connect()` → `get_datasource_info()` → `disconnect()`。

---

## 2. FileConnector（CSV / Parquet）

**文件：** `connector/file.py`

### 2.1 路径解析与加载

```
config.path
    ├── 单文件 → _read_single_file(path)
    │              table_name = path.stem（不含扩展名）
    └── 目录   → _read_directory(path)
                   glob("*.csv") + glob("*.parquet") + glob("*.pq")
                   每个文件 → 独立 table，name = stem
```

加载逻辑：

- `.csv` → `pd.read_csv(path)`，全量读入内存
- `.parquet` / `.pq` → `pd.read_parquet(path)`
- 结果存入 `self._dataframes: dict[str, pd.DataFrame]`

**注意：** 文件源是**全量加载**，不是流式；大文件会占用较多内存。采样发生在 `get_datasource_info()` 阶段。

### 2.2 Schema 推断（`_infer_columns`）

对每个 DataFrame 的每一列：

| 字段 | 算法 |
|------|------|
| `name` | `df.columns` 原样 |
| `dtype` | `_classify_dtype(series)` |
| `nullable` | `series.isna().any()` |
| `is_primary_key` | `nunique == len(df) and not nullable`（启发式：全唯一非空列视为 PK） |
| `comment` | 固定 `""`（文件无注释） |
| `foreign_keys` | 固定 `[]` |

### 2.3 类型分类（`_classify_dtype`）

按优先级检测：

1. Pandas 原生 dtype：`integer` / `float` / `boolean` / `datetime`
2. `object` / `string` 列进一步试探：
   - `pd.to_numeric(non_null)` 成功 → `numeric_string`
   - `pd.to_datetime(non_null)` 成功 → `datetime`
   - 否则 → `string`
3. 其他 → `str(series.dtype)`

### 2.4 采样策略

```python
sample_df = df.head(self.config.sample_size)  # 默认 1000
sample_data[name] = sample_df.to_dict(orient="records")
```

**特点：** 取**前 N 行**，非随机采样，结果可能受文件排序影响。

### 2.5 输出结构

每个文件对应一个 `TableInfo`：

```python
TableInfo(
    name=stem,
    schema_name="file",
    columns=[ColumnInfo, ...],
    foreign_keys=[],
    row_count=len(df),
    source=str(config.path),
)
```

`DatasourceInfo.sample_data` 为 `{table_name: [{col: val, ...}, ...]}`。

---

## 3. DatabaseConnector（关系型数据库）

**文件：** `connector/database.py`  
**依赖：** SQLAlchemy `create_engine` + `inspect()`

### 3.1 连接与 schema 选择

```python
self.engine = create_engine(config.connection_string)
```

支持所有有 SQLAlchemy 驱动的数据库（PostgreSQL、MySQL、SQLite 等），包括 `dialect+driver` 格式（如 `mysql+pymysql://`、`postgresql+psycopg2://`）。

**Schema 选择规则：**

| 数据库类型 | 默认 schema | URL `.schema` 后缀 | 行为 |
|---|---|---|---|
| PostgreSQL | `public` | 支持：`postgresql://user:pass@host/mydb.myschema` → schema=`myschema`，连接到 `mydb` | 指定 schema 后按该 schema 内省；不指定则用 `public` |
| MySQL/MariaDB | `None`（自动） | 不适用 | URL 中的 database 名即为 MySQL 库名，`Inspector.get_table_names(schema=None)` 列出当前库的所有表 |
| SQLite | `None` | 不适用 | SQLite 单文件无 schema 层级 |

**MySQL / SQLite 注意事项：** `DatasourceConfig.schema_name` 默认值为 `"public"`（PostgreSQL 约定），但 `DatabaseConnector.connect()` 会自动检测 dialect：对 MySQL/MariaDB/SQLite 将 `schema_name` 设为 `None`，避免查询不存在的 `public` schema 导致失败。SQLite 文件路径中的点（如 `.db`、`.sqlite`）也不会被 `_extract_schema_from_url()` 拆成 `db.schema`。

URL 中的 `db.schema` 格式通过 `_extract_schema_from_url()` 解析：如果连接串 path 包含 `.`（如 `/mydb.myschema`），则拆分为 database name 和 schema name，重建连接串只含 database name。MySQL/MariaDB/SQLite 跳过此拆分。

### 3.2 Schema 内省流程

对 `config.schema_name` 下每张表（PostgreSQL 默认 `public`，MySQL/SQLite 为 `None`）：

```
inspector.get_table_names(schema=config.schema_name)
    → 对每张 table_name:
        _extract_columns()
        _extract_foreign_keys()
        _get_row_count()          # SELECT COUNT(*) FROM {qualified_table_name}
        _get_table_comment()      # inspector.get_table_comment()
        get_sample_data(table, sample_size)
```

`_qualified_table_name()` 对非 `public` schema 的表名加上 schema 前缀（如 `myschema.orders`），`public` schema 和 `None` schema 则直接使用裸表名。**所有拼进原始 SQL 的标识符（表名/schema）都会按方言加引号**，因此带连字符的表名（如 `dacomp-zh-006`）可以正确执行 `COUNT(*)` 与采样查询。

### 3.3 列元数据（`_extract_columns`）

1. `inspector.get_pk_constraint()` → 得到 PK 列集合
2. `inspector.get_columns(table, schema)` → 每列：
   - `name`, `dtype`（SQLAlchemy 类型字符串）, `nullable`, `comment`
   - `is_primary_key = name in pk_columns`

### 3.4 外键提取（`_extract_foreign_keys`）

`inspector.get_foreign_keys()` 返回约束列表，每条转为：

```python
ForeignKeyInfo(
    constraint_name=fk["name"],
    source_table=table_name,
    source_column=fk["constrained_columns"][0],   # 仅取第一列（复合 FK 简化）
    target_table=fk["referred_table"],
    target_column=fk["referred_columns"][0],
)
```

这些 FK 会在 Extractor 阶段转为 `foreign_key` 边（confidence=1.0）。

### 3.5 随机采样（`get_sample_data`）

按方言选择排序子句：

| 方言 | SQL |
|------|-----|
| postgresql | `ORDER BY RANDOM()` |
| mysql | `ORDER BY RAND()` |
| sqlite | `ORDER BY RANDOM()` |
| 其他 | 无 ORDER（取前 N 行） |

```sql
SELECT * FROM {table_name} {order_clause} LIMIT {n}
```

通过 `pd.read_sql(text(query), engine)` 执行，转为 `list[dict]`。

失败时记录 warning 并返回 `[]`，该表后续 Profiling 会被跳过。

### 3.6 行数统计

```sql
SELECT COUNT(*) FROM {table_name}
```

注意：表名未加 schema 前缀，依赖数据库默认 search_path。

---

## 4. 数据流小结

```
DatasourceConfig
       │
       ▼
  connect() ──► 内存 DataFrame / SQLAlchemy Engine
       │
       ▼
get_datasource_info()
       │
       ├── tables[]: TableInfo（schema + FK + comments）
       └── sample_data{}: 每表最多 sample_size 行
       │
       ▼
  disconnect()
       │
       ▼
  DatasourceInfo ──► Extractor / Profiler
```

---

## 5. 已知限制与影响

| 限制 | 对下游的影响 |
|------|-------------|
| 文件源无 FK | 跨表关系完全依赖 JoinableInferrer |
| CSV 取 head 非随机 | Profile 统计可能有偏差 |
| 数据库采样失败返回空 | 该表无 Profile，不参与推断 |
| 复合 FK 只取第一列 | 多列 FK 可能不完整 |
| 文件全量加载 | 大 CSV 内存压力大 |
| `dialect+driver://` URL 需安装对应 Python driver | 如 `mysql+pymysql://` 需 `pip install pymysql`，否则 SQLAlchemy 连接失败 |
