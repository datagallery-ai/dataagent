-- step2_2: 动态特征聚合与衍生（`<warehouse>`）
-- 输入: step2_1_wide_joined + step2_0 分类出的全部 1:N 行为表 / 维度表
-- 输出: {{output_database}}.step2_2_wide_aggregated
-- 本文件整体必须作为一条独立 `<warehouse>` MCP command 提交。
-- 正文由 step2_2_expand_sql.py 按 references/aggregation_policy.json 确定性展开，禁止手写。
--
-- !!! warehouse GROUP BY 硬规则 !!!
-- `<warehouse>` 要求 GROUP BY 中直接引用原生列名，禁止对列名做任何函数包装。
-- 错误: GROUP BY toString(<user_id>)   →  `<warehouse>` 会报 NOT_AN_AGGREGATE
-- 正确: GROUP BY <user_id>              →  SELECT 中可以 toString(<user_id>)
-- 此规则对 toInt / toFloat / toDate / CAST 等所有类型转换函数均适用。
--
-- !!! warehouse 多层 JOIN 限制 列解析限制（已知版本 bug）!!!
-- 同一 FROM 子句里写多个 LEFT/INNER/CROSS JOIN（即使都是真实表、ON 已带表别名）
-- 会误报 Missing columns: '<user_id>'。USING 会报 Multiple USING。
-- 每层嵌套子查询恰好 1 个 JOIN 可通过。ARRAY JOIN 不受此限制。
-- 禁止为此错误创建 step2_2_test* / 物化诊断表 / 扁平重试。
--
-- 展开内容（每张 1:N 表一个按 <user_id> 聚合的 CTE；每张维度表单层 CROSS JOIN）：
-- 1. 每个可聚合列按 references/aggregation_policy.json 的类型规则产出唯一聚合表达式；
-- 2. delimiter 为 # / ^ 的字段在 CTE 内完成二元展开，原列不进入 CTE 输出；
-- 3. 不可聚合列（高基数 String、标识符、Array/Map/Tuple/Nested/Enum）不产出列；
-- 4. 最外层 EXCEPT 掉已展开的列表原列、已 CAST 列与地理原列，再写回表达式。

CREATE OR REPLACE TABLE {{output_database}}.step2_2_wide_aggregated
ENGINE = MergeTree
ORDER BY <user_id>
SETTINGS allow_nullable_key = 1
AS
/*__AGGREGATION_BODY__*/;
