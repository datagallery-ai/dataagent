---
name: sql_rules
description: |
  数仓生产 SQL（DDL/DML）的规范。Agent 编写、审核或修改 DDL/DML 时按需读取对应规则。
  不适用于：纯数据分析任务（不产出生产 SQL）、非 Hive/Spark 引擎的 SQL、口径/需求分析（见 instructions.md 编排部分）。
---

# SQL Rules（数仓生产规范）

## 文档内容

- `references/ddl_rules.md`：DDL 的建表语法规则。
- `references/dml_rules.md`：DML 的 INSERT 语法规则和通用规则。

## 读取原则

- 编写、修改或审核 DDL（`create_<table>.sql`）之前，使用 `read_file` 读取：

```
skill/sql_rules/references/ddl_rules.md
```

- 编写、修改或审核 DML（`insert_<table>.sql`）之前，使用 `read_file` 读取：

```
skill/sql_rules/references/dml_rules.md
```

- 同时交付 DDL 和 DML 时，先读取 DDL 规则编写 DDL；审核或修改 DML 前再读取 DML 规则，并核对两者的分区列集合和字段顺序。

## 使用约定

- 本 skill 目录为只读；生成的 DDL/DML 一律用 `write_file` 写入 workspace，禁止写入 skill 目录。
- DDL 专属规则维护在 `references/ddl_rules.md`，DML 与通用查询规则维护在 `references/dml_rules.md`。不要把规则复制进 instructions.md 或其他文档，避免多份漂移。
- 修改已有 DML 时遵循最小变更原则：只改规范所必需的部分，不重构无关逻辑。
