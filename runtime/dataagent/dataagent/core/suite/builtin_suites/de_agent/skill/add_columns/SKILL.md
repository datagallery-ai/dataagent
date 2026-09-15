---
name: add_columns
description: |
  当用户需要给已有数仓表新增列时使用此skill。适用于在现有表上追加新字段（ALTER TABLE ADD COLUMNS）并同步修改 INSERT 逻辑的场景。
  不适用于：修改已有列、从零建新表（走主流程 create+insert）。
---

# 加列 Skill（ALTER TABLE ADD COLUMNS）

> **前置要求**：执行本 skill 前，必须先确认已阅读并遵守 instructions.md 中的通用规则，本 skill 不重复上述内容。当发生冲突时，以本 skill 要求为准。

## 何时使用

- 用户明确要求给已有表添加新字段/新列
- 用户提到"加字段"、"新增列"、"追加特征到现有表"、"在 xxx 表上加一列"等意图
- 目标表已在数仓中存在，只需增量扩展列定义

## 最终交付件

最终交付物使用 `write_file` 写入 workspace 目录下，交付内容：
- 目标表 ALTER DDL 文件：`alter_<table_name>.sql` (Hive方言)
- 目标表 INSERT 文件：`modified_insert_<table_name>.sql` (Spark SQL方言)
- (可选) 前置原始表 ALTER DDL 文件：`alter_<table_name>.sql` (Hive方言)
- (可选) 前置原始表 INSERT 文件：`modified_insert_<table_name>.sql` (Spark SQL方言)

## 工作流

按照以下工作流生成并输出任务计划，计划必须包含所有步骤。

### Step 1: 确定目标表

- 完整表名由数据库名加表名构成({数据库名}.{表名})，两者均相同才算同一张表，否则视为不同表
- 如果用户提供了具体表名，调用 `validate_table_name` 工具校验表名是否合法；**如果表名不合法，要求用户确认**
- 如果用户未指定表名
  - 使用 `read_file` 读取本 skill 目录下的 `references/base_feature_tables.md`，根据用户需求关键词匹配最相关的表
  - 使用 `metadata_recall` 工具获取这些表的详细信息，分析后选择最相关的表
  - **目标表必须是 `base_feature_tables.md` 所列出的表**，如果无法确定目标表，要求用户确认

### Step 2: 查询已有表的元数据

- 使用 `check_table_existence` 工具检查目标表是否存在：该工具查询元数据服务并返回完整 schema
  - 如果返回 `exists=False`（表不存在）：**立即跳转”降级”章节，禁止重试、使用 `metadata_recall` 搜索、禁止跳过继续执行后续步骤**
  - 如果返回 `exists=True`：继续后续步骤，schema 信息已包含在返回结果中
- 使用 `query_sql_process` 工具获取目标表当前的 INSERT SQL，如果无法获取，要求用户提供当前表的 INSERT SQL，**禁止重试或跳过该步骤**

### Step 3: 检查前置原始表

- 使用 `read_file` 读取本 skill 目录下 `references/base_feature_tables.md`，如果目标表存在前置原始表（**数据库名与表名均相同才视为同一张表**），则：
  - 使用 `check_table_existence` 检查是否存在，如果不存在则告知用户并跳过该步骤
  - 使用 `query_sql_process` 获取前置原始表的 INSERT SQL，如果不存在则告知用户并跳过该步骤
  - 将前置原始表也纳入修改范围，后续步骤需同步处理
  - **前置原始表只能是文件中列出的表，禁止根据 SQL 内容推断前置表**

### Step 4：理解现有表的业务逻辑

- 基于前面步骤的表结构、INSERT SQL，完整理解现有表的业务逻辑
- 使用 `metadata_recall` 查询 SQL 中用到的其他的表、列、UDF 等元数据的详细信息，**禁止对这些表使用 `query_sql_process` 工具**

### Step 5: 生成修改方案

- 明确用户要新增的列的业务含义，与现有表、列的关系
- 确定新列的列名、类型等关键信息
- 优先基于 INSERT SQL 中已有的表开发新列
- 如现有表无法满足需求，使用 `metadata_recall` 工具获取所需的表、列等元数据信息
- 如需求不明确，主动向用户确认
- **关键约束**：只允许新增列，严禁修改或删除已有列的定义

### Step 6: 生成 ALTER TABLE DDL

- 根据已有表信息和新列定义，生成修改表结构 SQL
- 如果目标表有前置原始表，需要同时生成前置原始表的 ALTER TABLE ADD COLUMNS DDL
  - 前置原始表中新增的列是目标表新列的数据来源字段（通常是原始粒度的明细数据）
  - 目标表的新列是对前置原始表字段的聚合/转换结果
- DDL 格式（Hive 方言）：
```sql
ALTER TABLE {db}.{table_name} ADD COLUMNS (
  new_col1 STRING COMMENT '新列1的业务含义',
  new_col2 BIGINT COMMENT '新列2的业务含义'
);
```

### Step 7: 生成新的 DML

- 修改已有 INSERT SQL，将新列加入 SELECT 输出：
  - 修改现有的 SQL，**禁止**使用 `wrapped_nl2sql_sub_agent_tool` 生成
  - **最小变更原则**：只修改为新增列所必需的部分，严禁重构已有逻辑、调整已有字段顺序、重命名已有 CTE 等散弹式修改
  - 确保新列的位置与 ALTER TABLE ADD COLUMNS 中的列顺序一致
- 如果目标表有前置原始表，需要同时修改前置原始表的 INSERT SQL：
  - 前置原始表 DML 中新增的数据来源字段，是目标表 DML 中新列的输入
  - 同样遵循最小变更原则

### Step 8: 校验

- 交付件需包含 `alter_<table_name>.sql` 和 `modified_insert_<table_name>.sql`
- 如修改涉及前置原始表，需包含 `alter_<origin_table_name>.sql` 和 `modified_insert_<origin_table_name>.sql`
- 检查生成的 DDL/DML 是否满足用户需求
- 对照 `instructions.md` 中的相关规范对 DDL 和 DML 进行质量校验，跳过不适用规则（如 create table 相关规则）
- **`validate_deliverables`不适用本场景，禁止调用 `validate_deliverables` 工具进行校验**
- 如校验不通过，修正后重新调用直到通过

### Step 9: 输出变更 Diff

在回复中按如下格式输出变更内容：
```markdown
## 变更 Diff

目标表：{db_id}.{table_name}
前置原始表：{db_id}.{origin_table_name}(如无则写"无")

### DDL 变更（ALTER TABLE ADD COLUMNS）
新增列：
| # | 表名 | 列名 | 类型 | COMMENT |
|---|------|------|------|---------|
| 1 | {origin_table} | new_col1 | STRING | 业务含义（原始字段） |
| 2 | {target_table} | new_col2 | BIGINT | 业务含义（聚合结果） |

### DML 变更
**新增内容**：{描述每个表修改了哪些 CTE / SELECT / JOIN}
```

## 降级：目标表不存在时回退新建表流程

当 Step 1 中使用 `check_table_existence` 校验目标表时，如果返回 `exists=False`（表不存在）：
1. 告知用户目标表在元数据中未找到，无法执行加列操作
2. 询问用户是否需要改为创建新表
3. 如用户确认，退出本 skill，回到 instructions.md 主流程，按新建表逻辑生成 `create_*.sql` + `insert_*.sql`
4. 不自动降级，必须经用户确认
