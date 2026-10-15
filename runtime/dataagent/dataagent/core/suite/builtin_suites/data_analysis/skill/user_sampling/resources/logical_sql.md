# 逻辑 SQL 契约

步骤文档里的 SQL 描述关系运算。提交给仓之前，先读工作区 `data_source.md`，把仓名、资源 id、catalog 和库表占位符换成当前取值，再把下面的逻辑算子编译成当前仓能执行的语句。

## 逻辑算子

| 标记 | 语义 |
|---|---|
| `<LOGICAL_MATERIALIZE target="db.table">` | 把后续查询物化成目标表 |
| `<LOGICAL_LIST_TABLES>` / `<LOGICAL_LIST_COLUMNS>` | 查询当前仓的表清单或列清单 |
| `<LOGICAL_COUNT_TABLES>` | 按条件统计 catalog 中的表 |
| `<LOGICAL_COUNT_DISTINCT(expr)>` | 对非空表达式去重计数 |
| `<LOGICAL_COUNT_IF(predicate)>` | 统计满足条件的行 |
| `<LOGICAL_COUNT_DISTINCT_IF(expr, predicate)>` | 对满足条件的非空表达式去重计数 |
| `<LOGICAL_ROW_COUNT(relation)>` | 统计一个关系的行数 |
| `<LOGICAL_SAFE_DIVIDE(numerator, denominator)>` | 分母为 0 时结果为空，否则做除法 |
| `<LOGICAL_ANTI_JOIN right="..." on="...">` | 去掉能匹配右表的行，空键的处理与当前仓的 anti join 一致 |
| `<LOGICAL_STABLE_HASH(expr)>` | 对相同输入给出稳定、可复现的哈希 |
| `<LOGICAL_TAKE count="expr" order_by="expr">` | 按指定顺序最多取 count 行 |
| `<LOGICAL_PERCENTILE(expr, 0.9)>` | 计算指定分位数 |
| `<LOGICAL_DEDUPLICATE keys="..." order_by="...">` | 每个键保留排序后的第一行 |
| `<LOGICAL_PARSE_TIMESTAMP(expr)>` | 把字符串转成时间；无法解析时结果为空 |

同一个算子可以展开成函数、窗口、子查询或多层 CTE。展开后的语句和上表的语义一致。

## 提交前核对

按这个顺序核对，四项都完成后提交编译结果：

1. `{{source_database}}`、`{{output_database}}`、表名、列名和任务参数都已替换成 plan 或 schema 里的具体值。
2. 语句里不再有 `<LOGICAL_*>`、`<...>` 或 `{{...}}`。
3. 建表、覆盖、标识符引用、时间计算、类型转换和限量写法使用当前仓支持的语法。
4. 编译后的语句仍然是单条 `CREATE` 或 `SELECT`，符合 sampler 的语句白名单。

当前仓做不到与某个算子相同的语义时，写出阻塞说明并停止。提交的是编译后的语句，逻辑模板留在步骤文档里。
