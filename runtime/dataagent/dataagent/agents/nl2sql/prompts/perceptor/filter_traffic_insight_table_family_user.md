## Question:
{{ question }}

## Candidate Context

以下“候选表簇”中的 `family_name` 及其“表簇可用时间粒度”是唯一允许返回的值。

{{ tables }}

## 返回前强制自检

- 只选择上方候选中的一个 `family_name`。
- `granularity` 必须逐字复制自所选表簇自己的“表簇可用时间粒度”。
- 用户要求但候选中不存在的粒度只能用于判断回退方向，禁止把该不存在的粒度直接输出。
- 只按 system prompt 规定的 JSON 格式返回一个结果，不要输出分析或解释。
