-- step2_3: 动态特征聚合与衍生（ClickHouse）
-- 输入: step2_2_wide_cleaned + step2_0 分类出的全部 1:N / 时序 / 游戏维表
-- 输出: {{output_database}}.step2_3_wide_complete
-- 动态块按 schema_resolution 与画像结果生成，不能假定固定表数、字段名或业务取值。
-- 本文件整体必须作为一条独立 ClickHouse MCP command 提交。

CREATE OR REPLACE TABLE {{output_database}}.step2_3_wide_complete
ENGINE = MergeTree
ORDER BY <user_id>
AS
WITH
/*__DERIVATION_CTES__*/
SELECT
    w.*
    /*__DERIVED_SELECT_COLUMNS__*/
FROM {{output_database}}.step2_2_wide_cleaned AS w
/*__DERIVATION_JOIN_BLOCKS__*/;

-- 展开规则：
-- 1. 每张 1:N/时序表至少生成一个按 <user_id> 聚合的 CTE；
-- 2. 列表词表、分箱阈值、维表映射均来自全量画像并记录到 derivation 文档；
-- 3. 每个 CTE 必须在 SELECT 和 JOIN 动态块中各出现一次；
-- 4. 无动态衍生时移除 WITH 与三个动态块，仅保留 w.*，不能保留空模板。
