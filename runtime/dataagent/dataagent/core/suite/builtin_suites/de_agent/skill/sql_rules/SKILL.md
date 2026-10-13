---
name: sql_rules
description: |
  数仓生产 SQL（DDL/DML）的统一规范。编写或修改任何 DDL（建表）、INSERT（DML）之前查阅此 skill，据此生成并对照自检；
  不适用于：纯数据分析任务（不产出生产 SQL）、非 Hive/Spark 引擎的 SQL、口径/需求分析（见 instructions.md 编排部分）。
---

# SQL Rules（数仓生产规范）

## 何时查阅

- 写任何 SQL 之前 —— 先读「绝对禁令」「SQL 方言配置」两节
- 编写目标表 DDL（`create_<table>.sql`）或编写/修改 INSERT/DML（`insert_<table>.sql`）之前或之后 —— 查「通用 SQL 生成硬规则」「SQL 构造」两节
- 判断字段空值如何处理（过滤 / 回退 / 保留 / 补值）时 —— 查「空值处理矩阵」
- 对生成或修改后的 SQL 做质量自检时 —— 逐条对照适用规则

## 如何加载

使用 `read_file` 读取本 skill 的单一事实源：

```
skill/sql_rules/references/sql_rules.md
```

文档按用途分节，按当前任务缺口取用，不必一次内化全部：

- `## 绝对禁令（Zero-Tolerance，违反即交付失败）` —— 写任何 SQL 前必读
- `## SQL 方言配置（最高优先级）` —— DDL=Hive / DML=Spark，SparkSQL 3.1 版本约束
- `## 通用 SQL 生成硬规则（从历史问题抽象）` —— 可复用工程模式（序列、维度、UDF、时间窗口、分区、JOIN 等）
- `## 空值处理矩阵（必须先定策略再写 SQL）` —— 按字段角色选择过滤/回退/保留/补值策略
- `## SQL 构造` —— 语法结构、字段类型白名单、DECIMAL 精度、JOIN 类型

## 使用约定

- 本 skill 目录为只读；生成的 DDL/DML 一律用 `write_file` 写入 workspace，禁止写入 skill 目录。
- 规则条文只维护在 `references/sql_rules.md` 一处；不要把规则复制进 instructions.md 或其他文档，避免多份漂移。
- 修改已有 DML 时遵循最小变更原则：只改规范所必需的部分，不重构无关逻辑。
