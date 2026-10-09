# 在线查看和修改 SQL 规则 prompt 串讲

## 1. 功能简介

多场景下在线查看和修改 SQL 规则 prompt，改完不用重启进程，下一次查询立即读到新内容

- 入口：现有 `POST /api/agent/operation`，与 `type=query` 共用同一地址
- `type` 区分操作：`view`（查看）、`update`（更新）
  - view：读取全文和文档最新修改日期
  - update：整份替换原文件，文件写入路径固定，无缓存，改完无需重启
- `content.scenario` 选定场景，场景对应一份 yaml 和一份包内 md 文件
- 机制本身是通用的「包内可在线修改 md 文档」，不绑定 SQL 规则：哪些文档开放由代码里的开放清单（`dataagent/agents/nl2sql/utils/prompt_files.py` 的 `EDITABLE_PROMPTS` 注册表）唯一决定，当前只登记了各场景的 SQL 规则文件。后续开放新 md 文档时在清单里加一条即可；REST 请求暂不传文档名，固定操作默认文档

## 2. 特性入口

### 2.1 启动方式

启动入口与[【云核】北向接口修改 - WIKI]相同，一个进程挂多份 yaml

```bash
uv run -m dataagent \
  --config business_twin=dataagent/agents/nl2sql/business_twin.yaml \
  --config traffic_insight=dataagent/agents/nl2sql/traffic_insight.yaml \
  --host 127.0.0.1 \
  --port 8010
```

| 场景名 | yaml 路径 |
| --- | --- |
| `business_twin` | `dataagent/agents/nl2sql/business_twin.yaml` |
| `traffic_insight` | `dataagent/agents/nl2sql/traffic_insight.yaml` |

两个场景写入目标：

| `content.scenario` | yaml 里的配置 | 写入的文件 |
| --- | --- | --- |
| `business_twin` | `CORE.perceptor.user_sql_rules: sql_rules_business_twin` | `dataagent/agents/nl2sql/prompts/user/sql_rules_business_twin.md` |
| `traffic_insight` | `CORE.perceptor.user_sql_rules: sql_rules_traffic_insight` | `dataagent/agents/nl2sql/prompts/user/sql_rules_traffic_insight.md` |

- 写入目标由 yaml 里的 `CORE.perceptor.user_sql_rules` 决定，配置的是包内文件名（不带 `.md`，加载时自动补后缀），不是外部绝对路径；这条解析规则同样登记在 `EDITABLE_PROMPTS` 开放清单里（文档名 `sql_rules`）。
- 覆盖包内文件要求进程能写安装目录；镜像重建会恢复旧文件。生产环境改前建议先备份原文件。
- **前置条件**：view/update 只在多场景启动（`--config scenario=yaml`）下可用。单配置启动（`--config path` 不带等号）时没有场景名可寻址，view/update 一律 422（unknown scenario）；query 不受影响

### 2.2 操作一：查看 prompt（type=view）

`content.scenario` 选定场景，返回该场景`sql_rules_<scenario>.md`文件内容和最后修改时间。

请求：

```bash
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{"type": "view", "content": {"scenario": "business_twin"}}'
```

| 参数 | 说明 |
| --- | --- |
| `type` | `view` |
| `content.scenario` | 场景名，决定写入哪个文件 |

成功返回（HTTP 200，外层仍是 `{"result": ...}`）：

```json
{
  "result": {
    "success": true,
    "markdown": "(md全文)",
    "updated_at": "2026-09-30T08:15:23.123456+00:00"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `markdown` | 包内 md 文件原文，逐字返回 |
| `updated_at` | 该文件的最后修改时间（文件 mtime），UTC ISO 8601 格式，含微秒 |

### 2.3 操作二：更新 prompt（type=update）

`content.scenario` 选定场景，用 `content.markdown` 全文**整份替换**该场景`sql_rules_<scenario>.md`文件，写入位置由 `scenario` 固定，调用方不能另选

请求：

```json
curl -sS -X POST "http://127.0.0.1:8010/api/agent/operation" \
  -H "Content-Type: application/json" \
  -d '{"type": "update", "content": {"scenario": "business_twin", "markdown": "(md全文)"}}'
```

| 参数 | 说明 |
| --- | --- |
| `type` | `update` |
| `content.scenario` | 场景名，决定写入哪个文件 |
| `content.markdown` | 新全文，长度 **1–65536 字符**（不允许空串；按字符计，依据与影响见 §3 第 6 条） |

- `content.markdown` 为空串或超过 65536 字符返回 422，文件不会被写入。

客户端命令示例（`jq --rawfile` 读出本地 md 放进 `content.markdown`）：

```bash
jq -n --arg scenario business_twin --rawfile markdown ./sql_rules_business_twin.md \
  '{type:"update", content:{scenario:$scenario, markdown:$markdown}}' \
| curl -sS -X POST http://127.0.0.1:8010/api/agent/operation \
    -H 'Content-Type: application/json' \
    --data-binary @-
```

成功返回（HTTP 200）：

```json
{
  "result": {
    "success": true,
    "updated_at": "2026-09-30T08:20:01.123456+00:00"
  }
}
```

`updated_at` 是写入后文件的修改时间（UTC ISO 8601，含微秒）。

- 写入方式：同目录临时文件 + 原子替换，任意时刻文件都是某一次请求的完整全文；并发查询不会读到半截内容，并发更新后写覆盖先写。
- 生效机制：每次 query 的感知环节都会重新读 md 文件（进程内不缓存正文），所以 update 写完后**不必重启**，下一次 query 就生效。
- 影响面：`sql_rules` 不只喂给生成环节。它会被注入 generator、selector、validator 的用户 prompt（模板里都是 `{{ sql_rules }}`）。一次 update 影响所有消费该场景 `sql_rules` 的环节，不只是"下一次 query 的感知"。

## 3. 规格约束与边界

1. update 是整份替换：`content.markdown` 携带全文，服务端原样覆盖包内 md
2. 在线更新确实生效的机制：`PerceptorNode._aprocess` 每次感知都调用 `_load_prompt`，后者每次对目标文件执行 `read_text(encoding="utf-8")`，进程内不缓存正文。加载顺序是 绝对路径 → `<WORKSPACE.path>/<name>` → 包内 `nl2sql/prompts/user/<name>.md`。因此 update 写入后无需重启，下一次 query 即读到新内容；反向也成立：若 `<WORKSPACE.path>` 存在同名文件，加载顺序优先于包内文件，update 写入的包内文件会被盖过
3. view/update 两个操作的目标由 `scenario` 固定映射到包内文件，调用方不能指定别的路径；服务端也不打开调用方给的任何路径（请求里发的是全文，不是文件路径）。目标文件路径在进程内缓存：服务运行中修改 yaml 里的 `user_sql_rules` 值不会改变 view/update 的目标文件，重启进程后才切换。
4. `updated_at` 是目标文件的 mtime（UTC ISO 8601，含微秒）。view 返回当前文件 mtime；update 成功后也回写该值。它不是审计记录：安装/升级、镜像重建、绕过接口直接改文件都会刷新它，回答的是"当前这份内容何时落盘"。
5. 场景路由边界：请求里的 `scenario` 必须和启动参数 `--config` 等号左边的场景名一致。没有这项名字时返回 **422**，响应体与 query 相同的 `{"detail": "unknown scenario: <名字>"}`（两个操作在同一路径上行为一致）。同一个场景名在启动参数里出现两次，启动失败，后一次不覆盖前一次。
6. `content.markdown` 上限 65536 字符的依据与边界：
   - 65536 = 2^16，是防误传大文件的守卫值（如整份错误文件、二进制），不是链路功能极限；实现在请求模型 `UpdateContent.markdown` 的 `max_length`
   - 按**字符**计而非字节：中文 65536 字符约合 192KB（UTF-8 每汉字 3 字节），"64KB" 的说法只对纯 ASCII 成立
   - 传输链路其他层不卡这个量级：REST 中间件请求体上限 `rest_api.max_body_bytes` 默认 1MB（按字节计，yaml 可配），Python 字符串与文件写入无实质上限。65536 字符经 JSON 编码后最坏（全中文 + 转义）约 200KB，默认配置下永远是 422（`string_too_long`）先触发，语义是"校验拒绝、未写文件"；仅当 `max_body_bytes` 被配到约 200KB 以下才会先收到中间件的 413
   - **LLM 上下文影响（真实的功能性天花板）**：`sql_rules` 全文注入 generator、selector、validator 三个环节的用户 prompt，每次 query 都全量携带。当前最大场景文件约 1.6 万字符（约为上限的 1/4）；文件越接近上限，每次查询的 token 消耗、时延、成本线性上涨，过大还会挤占模型上下文。该上限因此同时是功能性守卫，不只是传输守卫
   - 后续调整是单点改动：`UpdateContent.markdown` 的 `max_length` + 对应测试常量 + 本文档；要支持超过 1MB 的请求体，还需调大 yaml 里的 `rest_api.max_body_bytes`

7. 错误码（不新增错误码，文件层失败复用现有 agent 失败包）：

   | 阶段 | 场景 | 请求示例 / 触发条件 | 期望 |
   | ----------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------- |
   | 进 agent 之前（请求体解析） | `type` 不是 `query`/`view`/`update` | `{"type": "other", "content": {"scenario": "business_twin"}}` | 422，错误类型 `union_tag_invalid` |
   | 进 agent 之前（请求体解析） | 传入了未声明的字段 | `{"type": "view", "content": {"scenario": "business_twin", "foo": 1}}` | 422，错误类型 `extra_forbidden` |
   | 进 agent 之前（请求体解析） | `content.markdown` 为空串或超过 65536 字符 | `content.markdown` 填空串 / 超长文本 | 422（`string_too_short` / `string_too_long`），文件不写入 |
   | 进 agent 之前（请求体解析） | `content.scenario` 缺失或空串 | 不传 `scenario` / 传 `""` | 422（`missing` / `string_too_short`） |
   | 场景路由 | 未登记的 `scenario` | 请求里的 `scenario` 未在启动参数登记 | 422，`{"detail": "unknown scenario: ..."}`，与 query 一致 |
   | 文件层（失败包） | 场景 yaml 未配置 `CORE.perceptor.user_sql_rules` | yaml 里删掉该键 | code `WORKFLOW-AGENT-001`，HTTP 500 |
   | 文件层（失败包） | yaml 配了带目录的名字 | `user_sql_rules: "../xxx"` | code `WORKFLOW-AGENT-001`，HTTP 500，文件不写入 |
   | 文件层（失败包） | 目标包内文件不存在（view 同样适用）/ 写入失败（如安装目录只读） | yaml 指向未随包发布的文件名 | code `WORKFLOW-AGENT-001`，HTTP 500，原文件保持完好 |

   失败包示例（文件层错误使用，外层仍是 `{"result": ...}`，`success: false`，字段与现有 operation 失败包一致：`code`、`message`、`http_status`、`component`、`retryable`）：

   ```json
   {
     "result": {
       "success": false,
       "code": "WORKFLOW-AGENT-001",
       "message": "CORE.perceptor.user_sql_rules is not configured: /path/to/business_twin.yaml",
       "http_status": 500,
       "component": "agent",
       "retryable": false
     }
   }
   ```

8. 扩展机制：本功能不写死 SQL 规则。可在线修改的 md 文档由 `dataagent/agents/nl2sql/utils/prompt_files.py` 的 `EDITABLE_PROMPTS` 开放清单唯一决定（当前仅 `sql_rules`，解析规则 = 场景 yaml 的 `CORE.perceptor.user_sql_rules` 键 → 包内 `agents/nl2sql/prompts/user/` 目录）。后续开放新文档：目标文件名仍由「yaml 某键 → 包内某目录」决定的，在清单里加一条即可；命名规则不同的，扩展 `EditablePromptSpec` 单点支持，并在 REST 请求模型里暴露文档选择参数。

## 4. 测试建议

前置：按 §2.1 多场景启动；单配置启动下 view/update 一律 422，属预期行为。以下按组列举用例点，期望值以 §2/§3 为准。

### 4.1 功能与生效

- view：返回的 `markdown` 与包内文件逐字一致（建议构造含中文、引号、反斜杠、代码块的内容）；`updated_at` 为 UTC ISO 8601 含微秒，与文件 mtime 一致
- update round-trip：写入后再 view，读回内容与提交的 `markdown` 完全一致，`updated_at` 等于写入后的 mtime
- 场景隔离：更新 `business_twin` 后，`traffic_insight` 的 view 内容与 `updated_at` 均不变
- 无需重启生效：update 一条对生成结果可观测的规则，下一次 query（不重启进程）行为即变化
- 请求构造：markdown 含引号/换行时用 §2.3 的 `jq --rawfile` 示例，手拼 JSON 易转义出错

### 4.2 边界值（markdown 长度）

| 输入 | 期望 |
| --- | --- |
| 空串 | 422 `string_too_short`，文件不写入 |
| 65536 字符（建议用中文全文） | 200 成功，读回一致 |
| 65537 字符 | 422 `string_too_long`，文件不写入 |

上限按字符计（依据见 §3 第 6 条）：65536 个中文字符的请求体约 192KB，仍远小于 1MB 中间件上限，应先收到 422 而非 413。

### 4.3 请求校验与场景路由（422）

- `type` 为 `query`/`view`/`update` 之外的值 → `union_tag_invalid`
- content 里加未声明字段 → `extra_forbidden`
- `content.scenario` 缺失 / 空串 → `missing` / `string_too_short`
- 未登记的 `scenario` → 422 `{"detail": "unknown scenario: ..."}`，与 query 的响应体格式一致（可两操作对比）

### 4.4 文件层失败（500 失败包）

触发条件与期望见 §3 第 7 条错误表，重点覆盖：yaml 删掉 `CORE.perceptor.user_sql_rules` 键、配带目录的名字（`../xxx`）、指向不存在的文件名、安装目录只读。每项确认响应是完整的 `{"result": {"success": false, "code": "WORKFLOW-AGENT-001", ...}}` 失败包，且原文件未被写入 / 保持完好。

### 4.5 并发与一致性

- 并发两个 update：最终文件是其中一次请求的完整全文，无交叉、无半截；`updated_at` 对应后完成的那次（后写覆盖先写）
- update 进行中并发 query：读到的规则要么全旧要么全新，不会读到半截（原子替换）

### 4.6 预期现象（免责，非缺陷）

- `<WORKSPACE.path>` 下存在同名 md 时加载优先级高于包内文件：update 后 query 不生效属预期（见 §3 第 2 条）
- 服务运行中改 yaml 的 `user_sql_rules` 值不改变 view/update 目标（进程内缓存），需重启（见 §3 第 3 条）
- `updated_at` 不是审计记录：镜像重建、绕过接口手改文件都会刷新它；镜像重建后 view 内容回到镜像内版本（见 §3 第 4 条）

### 4.7 自动化回归入口

| 文件 | 覆盖 |
| --- | --- |
| `tests/ut/interface/test_rest_api_app.py` | 端点层：请求校验、422/500 映射、未知场景 |
| `tests/ut/interface/test_rest_api_prompt_service.py` | service 层：view/update 成功、未知场景、文件层失败包 |
| `tests/ut/agents/nl2sql/test_prompt_files.py` | 文件层：路径解析、裸文件名校验、原子替换 |

```bash
uv run pytest tests/ut/interface/test_rest_api_app.py \
  tests/ut/interface/test_rest_api_prompt_service.py \
  tests/ut/agents/nl2sql/test_prompt_files.py
```
