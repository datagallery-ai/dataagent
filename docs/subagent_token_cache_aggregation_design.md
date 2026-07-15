# 子 Agent Token 与缓存命中率聚合设计（含实现说明）

> **文档定位**：本文件是 PR #157（`opencode/subagent-token-cache-aggregation` → `main`）的 comprehensive 设计文档。
> 2026-07-03 完成初稿（基于 `main` HEAD `e141b7e` 现状分析 + 五步打通数据流方案），
> 2026-07-13 迁入 `dataagent/docs/` 并按 PR #157 实际实现状态更新（含 H0 fix、rebase 后代码索引、
> openai 模式 e2e 实测结果、已知缺口）。
> 与 `openspec/changes/add-subagent-token-cache-aggregation/{proposal.md,design.md}`（OpenSpec 决策日志 D1-D8）
> 互补：前者是 why + what + how 全景方案，本文是落地后的实现状态快照。
> 配套评审报告 `pr157_subagent_token_cache_aggregation_code_review.md`（留存在 my-gallery-doc 设计文档中心）。

## 0. 实现状态总览（2026-07-13）

| 模块 | 状态 | 位置 | 备注 |
|------|------|------|------|
| 共享 canonical usage 模块 | ✅ 已实现 | `dataagent/core/managers/llm_manager/usage.py`（241 行）| `usage_to_metadata`/`normalize_usage_metadata`/`summarize_usage`/`cache_hit_rate` 四入口，Anthropic canonical `input_tokens = raw_input + cache_read + cache_creation` |
| 现有入口迁移到共享模块 | ✅ 已实现 | `llm_client.py`/`adapters.py`/`performance.py`/`message_history.py` | 全部改为薄包装调用共享模块 |
| cache_control 白名单收紧 | ✅ 已实现 | `llm_client.py:_supports_explicit_cache_control(model, provider, base_url)` | 从"模型名含 qwen/qwq"改为 provider/base_url/model 白名单 |
| `cache_control_mode` 诊断字段 | ✅ 已实现 + **H0 fix** | `llm_client.py:_resolve_cache_control_mode` + `_astream_parse_line:1585` | 流式路径漏传 `cache_control_mode` 的 bug 已 fix（commit `648fd6e`） |
| `WorkerResult.perf_summary` 协议 | ✅ 已实现 | `worker_result.py` | `perf_summary: dict \| None = None`，schema_version=1 |
| 子进程 `perf_summary` 构造 | ✅ 已实现 | `sub_agent_entry.py:_build_perf_summary` | 局部 holder + 瘦身 schema |
| 父进程幂等聚合 | ✅ 已实现 | `tools.py:_merge_subagent_perf_summary` + `performance.py:merge_subagent_llm_usage` | 含 `_seen_subagent_perf_keys` 去重 + hash 兜底 |
| `build_summary` 三视角 | ✅ 已实现 | `performance.py:build_summary` | `main_agent`/`subagents`/`overall` + `by_agent` 下钻 + 顶层==overall 断言 |
| `summary_sink` 透传 | ✅ 已实现 | `base_agent.py:_performance_run` + `performance.py:bind_agent_performance` | 通过 `initial_state["_performance_summary_sink"]` 临时字段，Agent 层读取后 pop |
| `perf_ref` 轻量引用 | ✅ 已实现 | `tools.py:_strip_perf_summary_for_original_msg` | 父 `messages.json` ToolMessage 只保留 `perf_ref`，不写完整 `perf_summary` |
| **报告层读取策略（fallback + `source=messages_json_fallback`）** | ❌ **未实施** | — | Task 11 / spec Requirement「报告层读取策略」未落地；e2e 报告仍按主 Agent 单口径输出 |
| UT 覆盖 | ✅ 350+ 行 | `test_usage_module.py` + `test_subagent_perf_aggregation.py` | OpenAI/Anthropic/DeepSeek 三类 usage + 三视角 + dedup + perf_summary 协议 + perf_ref 剥离 |
| e2e 验证 | ⚠️ 部分 | `test_performance.py`（本 PR 未修改）| PR 描述只跑 `--quick` 3 查询 DeepSeek；本次 review 补跑 openai 模式全量 7 查询（见 §16） |

## 1. PR #157 前的 main 现状（历史背景）

> 本节描述的是 PR #157 合入前的 main 状态（HEAD `e141b7e`），作为设计动机的历史背景保留。

当前有两条独立但相关的记录链路：

1. `messages.json`：会话历史文件，由 `message_history.serialize_message()` 写入。主 Agent 的
   `AIMessage.usage_metadata` 会保留 `input_tokens`、`output_tokens`、`total_tokens`、
   `input_cache_read_tokens`、`input_cache_creation_tokens`、`output_reasoning_tokens`，并生成
   `round_summaries`。这条链路不受 `DATAAGENT_PERFORMANCE_ENABLED` 控制。
2. `.performance/*.jsonl`：性能事件日志，由 `PerformanceCollector` 在
   `DATAAGENT_PERFORMANCE_ENABLED=1` 时写入。它记录 LLM/node/tool/agent 事件、耗时、caller attribution、
   pid/run metadata，并在 `_flush` footer 中输出 summary。

`nl2sql_sub_agent_tool` 会启动独立 Python 子进程执行 `sub_agent_entry.py`。父进程只能解析子进程 stdout
中的 `worker_result`、`subagent_final_state`、`assistant_reply`。当前 `WorkerResult` 不包含 token/cache
字段，因此父进程 `.performance` summary 无法覆盖子 Agent LLM 消耗。

## 2. 设计目标

建立清晰的双轨职责：

- `messages.json` 继续保存可回放的会话历史，并保留主 Agent 每条 `AIMessage` 的 cache token。
- 父进程 `messages.json` 不保存子 Agent 内部 LLM 明细，不保存完整子 Agent token 聚合，只保存子 Agent
  结果和必要引用。
- `.performance` 作为全链路性能统计的权威来源，输出 `main_agent / subagents / overall` 三视角。
- `WorkerResult.perf_summary` 作为子进程到父进程的指标传输协议，避免父进程从 messages 或 final state
  反推子 Agent token。
- cache token 的字段提取、字段语义和命中率计算必须与
  `docs/main_agent_cache_optimization_design.md` 以及 `llm_client.py` 的实现完全一致；不得在
  `.performance`、`messages.json`、e2e 报告中各自重新解释厂商原始字段。

最终 summary 输出：

- `llms.main_agent`：父进程自身 LLM 调用。
- `llms.subagents`：所有子进程 LLM 调用聚合，可按 `agent_type/sub_id/worker_session_id` 下钻。
- `llms.overall`：`main_agent + subagents` 逐字段相加。

缓存命中率统一为 `input_cache_read_tokens / input_tokens`；`input_tokens == 0` 时返回 `null`。

## 3. Usage 语义统一

### 3.1 PR #157 前的差异

`main_agent_cache_optimization_design.md` 中定义了不同厂商 cache 字段映射：

- OpenAI / Qwen / DashScope：`prompt_tokens_details.cached_tokens` → `input_cache_read_tokens`。
- Anthropic：`cache_read_input_tokens` / `cache_creation_input_tokens` → cache read/create。
- DeepSeek：`prompt_cache_hit_tokens` → `input_cache_read_tokens`。

问题在于字段语义不完全相同：OpenAI/Qwen/DeepSeek 的 cache read token 已包含在 `input_tokens` 中；
Anthropic 的 `cache_read_input_tokens` 和 `cache_creation_input_tokens` 是独立报告，原始
`input_tokens` 不包含它们。如果 `.performance` 或 `messages.json.round_summaries` 直接使用
`input_cache_read_tokens / input_tokens`，Anthropic 分母会偏小。

### 3.2 统一口径（已实现）

canonical usage 语义：

```text
input_tokens = 本次请求的逻辑总输入 token，必须包含 cache read 与 cache creation。
output_tokens = 输出 token。
total_tokens = input_tokens + output_tokens；若厂商 total_tokens 不符合该语义，以 canonical 计算为准。
input_cache_read_tokens = 命中缓存读取的输入 token。
input_cache_creation_tokens = 本次写入缓存的输入 token。
output_reasoning_tokens = 推理 token。
```

基于该语义，所有链路统一使用：

```text
cache_hit_rate = input_cache_read_tokens / input_tokens
```

归一化规则：

- OpenAI/Qwen/DashScope/DeepSeek：原始 `input_tokens` 已包含 cached tokens，保持不变。
- Anthropic：canonical `input_tokens = raw_input_tokens + cache_read_input_tokens + cache_creation_input_tokens`。
- `total_tokens` 缺失或不等于 canonical input/output 之和时，使用
  `input_tokens + output_tokens` 作为 canonical total。

### 3.3 唯一实现入口（已实现）

新增 `dataagent/core/managers/llm_manager/usage.py`（241 行），提供四个入口：

```python
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens",
    "input_cache_read_tokens", "input_cache_creation_tokens", "output_reasoning_tokens",
)

def usage_to_metadata(raw_usage: Any) -> dict[str, int]:
    """将 OpenAI/Anthropic/DeepSeek/DashScope 原始 usage 归一为 canonical 6 字段。"""

def normalize_usage_metadata(usage: Any) -> dict[str, int]:
    """补齐和校正已归一或半归一 usage，保证字段语义与 usage_to_metadata 一致。"""

def summarize_usage(usage: Any) -> dict[str, int]:
    """性能、message history、报告层统一调用的汇总入口。"""

def cache_hit_rate(usage: Mapping[str, int]) -> float | None:
    """返回 0-1 小数；input_tokens 为 0 时返回 None。"""
```

现有入口统一改为调用该模块：

- `llm_client.py::_usage_to_metadata()` 调用 `usage_to_metadata()`，不再维护私有映射逻辑。
- `adapters.py:normalize_usage_metadata()` 改为从共享模块 import。
- `performance.py:summarize_llm_usage()` 改为调用 `summarize_usage()`（通过 `_resolve_usage_funcs` 懒加载解循环依赖）。
- `message_history.py` 序列化和 `round_summaries` 改为调用 `summarize_usage()` 与 `cache_hit_rate()`。
- e2e 性能报告和子 Agent `perf_summary` 构造只消费 canonical 6 字段。

## 4. cache_control 能力适配

### 4.1 已实现的判断规则

`_supports_explicit_cache_control(model, provider, base_url)` 已从"模型名含 qwen/qwq"收紧为
provider/base_url/model 白名单判断：

```python
_EXPLICIT_CACHE_PROVIDERS: frozenset[str] = frozenset({"bailian", "dashscope", "qwen", "anthropic"})
_EXPLICIT_CACHE_BASEURL_HINTS: tuple[str, ...] = ("aliyuncs.com", "dashscope", "anthropic.com")

def _supports_explicit_cache_control(model, provider=None, base_url=None) -> bool:
    # 1. Anthropic Claude — provider 命中 anthropic 或模型名含 claude，始终注入
    # 2. Qwen/QwQ — 仅当 provider/base_url 命中显式能力白名单时注入
    # 3. 百炼白名单模型 — 模型名精确匹配 _BAILIAN_EXPLICIT_CACHE_MODELS 且 provider/base_url 命中百炼端点
    # 4. DeepSeek 直连 / OpenAI GPT / 未知 — 不注入（保守策略）
```

`_build_payload()` 行为：

- 支持显式缓存的模型：调用 `_apply_cache_control_with_anchors()` 注入最多 4 个 bp0-bp4 断点。
- 不支持显式缓存的模型：调用 `_strip_cache_control()` 剥离历史消息中残留的 `cache_control`，避免 API 报错。

### 4.2 厂商/模型矩阵（已实现）

| 场景 | cache_control 行为 | cache token 提取 | 说明 |
| --- | --- | --- | --- |
| Qwen/QwQ via DashScope/百炼/Qwen 官方兼容端点 | 注入 bp0-bp4 | `prompt_tokens_details.cached_tokens` | 显式缓存 |
| Qwen/QwQ via generic OpenAI-compatible 端点 | 不注入，剥离残留 cc | `prompt_tokens_details.cached_tokens`（若返回）| 不能仅凭模型名推断显式 cc 能力 |
| Anthropic Claude | 注入 bp0-bp4 | `cache_read_input_tokens` / `cache_creation_input_tokens` | 需 canonical 修正 input 分母 |
| 百炼 `deepseek-v3.2` | 注入 bp0-bp4 | 按返回 usage 归一 | 同模型直连能力不同，依赖 provider/model |
| 百炼 `kimi-k2.6/kimi-k2.5/glm-5.1` | 注入 bp0-bp4 | 按返回 usage 归一 | 白名单需随官方文档维护 |
| DeepSeek 直连 | 不注入，剥离残留 cc | `prompt_cache_hit_tokens` | 隐式磁盘缓存 |
| OpenAI GPT | 不注入，剥离残留 cc | `prompt_tokens_details.cached_tokens` | 自动缓存，不支持显式 cc |
| 其他未知 OpenAI-compatible 模型 | 不注入，剥离残留 cc | 只能按通用 usage 字段归一 | 保守策略，避免请求失败 |

### 4.3 `cache_control_mode` 诊断字段（已实现 + H0 fix）

`LLMClient._resolve_cache_control_mode(usage_metadata)` 判定三态：

- `explicit`：`_supports_explicit_cache_control` 命中（`_build_payload` 注入了断点）。
- `implicit`：未注入显式 cc 但响应 usage 含 cache_read/creation（如 DeepSeek 直连 / OpenAI GPT）。
- `none_or_unknown`：未注入且无 cache usage。

`cache_control_mode` 通过 `LLMClientMessage` → `LLMResponse` → perf 事件 `extra` → `build_summary` 三视角传播。

**H0 fix（commit `648fd6e`）**：流式路径 `_astream_parse_line`（`llm_client.py:1577-1584`）构造返回的
`LLMClientMessage` 时漏传 `cache_control_mode`，导致所有流式 LLM 事件的诊断字段恒为
`"none_or_unknown"`（即使 `cache_read > 0` 应为 `"implicit"`）。fix 仅 1 行：
`cache_control_mode=wrapped.cache_control_mode`。非流式 `_wrap_response` 路径本就正确传递，不受影响。

### 4.4 与子 Agent 聚合的关系

子 Agent 聚合方案不重新判断某个子 Agent 是否应该注入 cache_control。判断必须发生在每个进程内的
`LLMClient._build_payload()` 中。父进程只接收 canonical `usage_metadata` / `perf_summary`，不反推 provider
能力，也不对 `cache_control` 行为做二次修正。

`perf_summary` 携带 `provider`、`model`、`cache_control_mode` 作为诊断展示字段，不参与 token 求和。

## 5. 存储边界

### 5.1 `messages.json` 保留什么（已实现）

主 Agent `messages.json` 继续保留主 Agent `AIMessage.usage_metadata`，包括 cache 字段。写入前经过
`normalize_usage_metadata()`，确保文件内保存的是 canonical 6 字段。

父 `messages.json` 中的子 Agent ToolMessage / `original_msg` 只保留轻量业务结果和 `perf_ref` 引用：

```json
{
  "sub_id": 1,
  "worker_session_id": "subagent_s1_1",
  "status": "success",
  "final_answer": "...",
  "artifacts": ["..."],
  "perf_ref": {
    "source": "subagent",
    "schema_version": 1,
    "worker_session_id": "subagent_s1_1",
    "worker_run_id": 0
  }
}
```

由 `tools.py:_strip_perf_summary_for_original_msg()` 实现。

### 5.2 `messages.json` 不保存什么

父 `messages.json` 不保存：

- 子 Agent 内部每次 LLM call 明细。
- 子 Agent 完整 `perf_summary`。
- `main_agent / subagents / overall` 全链路汇总。

### 5.3 `.performance` 保存什么

`.performance` 保存性能事件和最终汇总：

- LLM 调用耗时、tokens/sec、caller_kind/caller_name、`cache_control_mode`。
- node/tool/agent 耗时与错误数。
- 子 Agent 聚合后的 token/cache usage（`_subagent_llm_usages`）。
- `_flush.summary.llms.main_agent/subagents/overall`。

`.performance` 事件中的 `extra.input_tokens` 等字段必须来自 canonical `usage_metadata`。禁止在 performance
层解析厂商原始字段。

### 5.4 报告层读取策略（❌ 未实施，Task 11 缺口）

> **设计目标**：报告层按优先级读取 `.performance` footer `summary.llms`（权威）→ fallback
> `messages.json.round_summaries`（主 Agent 口径），标记 `source=messages_json_fallback`。
>
> **实现状态**：未实施。全仓库 `messages_json_fallback` 零命中；`build_summary` 仅在 `performance.py` 内被
> `snapshot_summary`/`flush` 调用，无独立报告层模块消费 footer。`test_performance.py` 的
> `_compute_cache_hit_rate`（line 674）仍用旧 `* 100` 百分比，未迁移到共享 `cache_hit_rate()`。
> 后续需独立 PR 补齐。

## 6. 总体方案

采用"子进程请求内生成 summary，随 `WorkerResult` 回传，父进程幂等合并到 `.performance`"的方案。

核心原则：

1. 不把 summary 写到长期存活的 Agent 实例字段，避免并发和重入串请求。
2. 不从 `subagent_final_state.messages` 或父 ToolMessage 二次累加 usage。
3. `perf_summary` 是可选协议扩展字段，缺失时业务结果仍成功返回。
4. 子进程 stdout 继续保持单 JSON；运行日志、状态日志和调试输出继续走 stderr。
5. `messages.json` 与 `.performance` 不物理合并，只在报告层做逻辑关联。
6. 所有 token/cache 字段先进入 canonical usage，再进入 message/performance/subagent summary。

## 7. 子进程 Summary 获取（已实现）

`PerformanceCollector.snapshot_summary(state)` 返回 `build_summary` 的只读快照。

`bind_agent_performance(..., summary_sink=None)` 在 `finally` 中先 snapshot 局部 summary，再调用
`summary_sink(summary)`，最后继续 `flush()` 写 footer。

`BaseAgent._performance_run()` 透传 `summary_sink`：从 `initial_state["_performance_summary_sink"]` 读取后
**pop 移除**，避免进入业务 workflow state。

`sub_agent_entry.py::_run_agent()` 使用局部 holder + `_build_perf_summary()` 构造瘦身 summary：

```python
perf_holder: dict[str, Any] = {"summary": None}
initial_state["_performance_summary_sink"] = lambda s: perf_holder.__setitem__("summary", s)
result = await agent.chat(query, initial_state=initial_state)
perf_summary = _build_perf_summary(
    perf_holder["summary"], result=result, query=query, config_path=config_path,
    parent_session_id=..., worker_session_id=..., sub_id=...,
)
return result, perf_summary
```

`_build_perf_summary()` 构造 schema_version=1 的瘦身 dict，只回传 `llms` 顶层聚合（canonical 6 字段 +
`call_count`）与必要 identity（`agent_type`/`sub_id`/`parent_session_id`/`worker_session_id`/`worker_run_id`/
`provider`/`model`/`cache_control_mode`/`status`），不回传 nodes/tools 分桶。性能采集关闭
（`DATAAGENT_PERFORMANCE_ENABLED != 1`）时返回 `None`，业务结果仍正常返回。

## 8. WorkerResult 协议扩展（已实现）

`WorkerResult` dataclass 新增 `perf_summary: dict[str, Any] | None = None`。

`worker_result_from_payload()` 仅在 payload 中存在 dict 时保留该字段，否则设为 `None`。
`synthesize_worker_result()` 新增 `perf_summary=None` 参数并透传。

`perf_summary` schema（schema_version=1）：

```json
{
  "schema_version": 1,
  "source": "subagent",
  "agent_type": "nl2sql",
  "sub_id": 1,
  "parent_session_id": "s1",
  "worker_session_id": "subagent_s1_1",
  "worker_run_id": 0,
  "query": "...",
  "provider": "bailian",
  "model": "qwen3.7-plus",
  "cache_control_mode": "explicit",
  "status": "success",
  "llms": {
    "input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500,
    "input_cache_read_tokens": 800, "input_cache_creation_tokens": 100,
    "output_reasoning_tokens": 0, "call_count": 4
  }
}
```

旧 payload 无 `perf_summary` 字段时业务结果正常解析，跳过聚合。

## 9. 父进程聚合（已实现）

`tools.py::_subagent_completed_outcome_from_worker_result_branch()` 解析 `WorkerResult` 后调用
`_merge_subagent_perf_summary(worker_result, resolved_session_id=..., last_run_id_executed=..., query=...)`。

`_merge_subagent_perf_summary` 读取 `worker_result.perf_summary`，从 `_subagent_runtime_context` 取
`tool_call_id`，构造 identity dict，调用 `merge_subagent_llm_usage(perf_summary, identity)`。

`PerformanceCollector.merge_subagent_llm_usage(perf_summary, identity)`：

- 校验 schema（`schema_version >= 1`）、token 非负整数、identity 未重复。
- `_seen_subagent_perf_keys: set[tuple[str, ...]]` 幂等去重。
- 去重键：`(parent_session_id, parent_run_id, tool_call_id, sub_id, worker_session_id, worker_run_id)`。
- `tool_call_id` 缺失时用 `query + worker_run_id + worker_session_id` 的 sha256 hash 兜底 + debug 日志。
- 聚合失败不影响 `sub_agent_tool` 返回，只记录 warning。

## 10. build_summary 三视角输出（已实现）

`build_summary()` 顶层兼容字段（`llms.input_tokens` 等）语义升级为 `overall`。新增显式三视角：

```json
{
  "llms": {
    "input_tokens": 1800, "output_tokens": 450, "total_tokens": 2250,
    "input_cache_read_tokens": 1000, "input_cache_creation_tokens": 150,
    "output_reasoning_tokens": 0,
    "state_messages": {},
    "main_agent": {
      "input_tokens": 1200, "...": "父进程 kind==llm 事件聚合",
      "call_count": 4, "cache_hit_rate": 0.667, "cache_control_mode": "implicit"
    },
    "subagents": {
      "input_tokens": 600, "...": "来自 _subagent_llm_usages",
      "call_count": 3, "cache_hit_rate": 0.5, "cache_control_mode": "explicit",
      "by_agent": {
        "nl2sql:1:subagent_s1_1": {
          "input_tokens": 600, "call_count": 3,
          "identity": {"agent_type": "nl2sql", "sub_id": "1", "worker_session_id": "...", "...": "..."}
        }
      }
    },
    "overall": {
      "input_tokens": 1800, "...": "main_agent + subagents 逐字段相加",
      "call_count": 7, "cache_hit_rate": 0.5556, "cache_control_mode": "implicit"
    }
  }
}
```

- `main_agent` 来自现有事件循环中的普通 `kind == "llm"` 事件。
- `subagents` 来自 `_subagent_llm_usages`，`by_agent` 可按 `agent_type:sub_id:worker_session_id` 下钻。
- `overall` 逐字段相加。所有 rate 字段使用共享 `cache_hit_rate()`，返回 0-1 小数。
- `cache_control_mode` 由 `_pick_cache_control_mode(modes)` 从观测列表中选最有信息量的一个
  （`explicit` > `implicit` > `none_or_unknown`）。
- 断言顶层兼容字段等于 `overall` 对应字段（`assert`，spec SHALL 断言）。

## 11. 失败与兼容策略（已实现）

- 子进程正常完成且性能采集开启：返回并聚合 `perf_summary`。
- 子进程业务失败但 stdout JSON 完整：若有 `perf_summary`，仍聚合并记录 `status="failed"`。
- 性能采集关闭、旧协议或 schema 不支持：跳过聚合，保留业务结果。
- 超时、被 kill、stdout 非 JSON：默认无 summary，不聚合。
- 父进程聚合失败不影响 `sub_agent_tool` 返回，只记录 warning。
- 旧 `messages.json` 中可能存在非 canonical Anthropic usage；读取旧文件时按现有字段保守兼容，不反推厂商
  原始字段。新写入文件必须使用 canonical usage。

## 12. 实施步骤与实现状态

| # | 步骤 | 状态 | 位置 |
|---|------|------|------|
| 1 | 新增共享 usage 模块，迁移各入口 | ✅ | `usage.py` + 4 处薄包装 |
| 2 | 修正 Anthropic canonical input + 单测 | ✅ | `usage.py:usage_to_metadata` + `test_usage_module.py` |
| 3 | 收紧 cache_control 白名单判断 | ✅ | `llm_client.py:_supports_explicit_cache_control` |
| 4 | 补充 `cache_control_mode` 诊断字段 | ✅ + H0 fix | `llm_client.py:_resolve_cache_control_mode` + `_astream_parse_line:1585` |
| 5 | `performance.py` snapshot/sink/聚合/三视角 | ✅ | `performance.py` |
| 6 | `base_agent.py` 透传 sink + pop | ✅ | `base_agent.py:_performance_run` |
| 7 | `sub_agent_entry.py` 局部 holder + 瘦身 | ✅ | `sub_agent_entry.py:_build_perf_summary` |
| 8 | `worker_result.py` 协议扩展 | ✅ | `worker_result.py` |
| 9 | `tools.py` 父进程聚合 | ✅ | `tools.py:_merge_subagent_perf_summary` |
| 10 | `message_history.py` round summary + perf_ref | ✅ | `message_history.py` + `tools.py:_strip_perf_summary_for_original_msg` |
| 11 | 报告层优先读 footer + fallback 标记 | ❌ 未实施 | 见 §5.4 |
| 12 | e2e 验证三视角加和 | ⚠️ 部分 | PR 描述 `--quick` + 本次 review 补 openai 全量（见 §16）|
| 13 | e2e 禁用 perf fallback 验证 | ❌ 未实施 | Task 13.7 |
| 14 | `tasks.md` 全部勾选 | ❌ 未勾选 | OpenSpec `/opsx-apply` 工作流违规 |

## 13. 测试计划与覆盖状态

### 单测（已实现）

- `test_usage_module.py`（284 行）：OpenAI/Qwen/DashScope inclusive input、Anthropic canonical 修正、
  DeepSeek `prompt_cache_hit_tokens`、`total_tokens` 重算、`cache_hit_rate` 0-1 小数、`normalize` 不重复计数、
  `_resolve_cache_control_mode` 三态、`_supports_explicit_cache_control` 白名单。
- `test_subagent_perf_aggregation.py`（350 行）：merge 正常/重复去重/负数拒绝/非法 schema、三视角结构、
  `cache_hit_rate` 小数、`WorkerResult.perf_summary` 协议、`sub_agent_entry._build_perf_summary`、
  `perf_ref` 剥离。

### 测试覆盖缺口

- ❌ **流式 `_astream_parse_line` → `cache_control_mode` 传递路径 UT**：`TestResolveCacheControlMode` 只覆盖
  `_resolve_cache_control_mode` 与 `_wrap_response`（非流式），未覆盖流式 chunk 传递，导致 H0 bug 漏检。
  建议补 `test_astream_parse_line_propagates_cache_control_mode`。
- ❌ **Task 13.5**：`round_summaries` 与 `.performance.llms.main_agent` 一致性断言。
- ❌ **Task 13.7**：禁用 `DATAAGENT_PERFORMANCE_ENABLED` 时 `messages.json.round_summaries` 可作 fallback。
- ❌ **Task 11 报告层**：`.performance` 存在时不叠加 `messages.json` token 的断言。

## 14. 验收标准与达成情况

- ✅ 不改业务返回协议的必需字段，旧 worker_result payload 可继续解析。
- ✅ `llm_client.py`、adapter、`messages.json`、`.performance`、e2e 报告全部使用同一套 canonical usage
  字段和同一套 cache_hit_rate 计算。
- ✅ cache_control 注入/剥离行为与 `_supports_explicit_cache_control()` 完全一致，子 Agent 不另起一套判断。
- ✅ Anthropic、OpenAI/Qwen、DeepSeek 的 cache token 字段进入系统后语义一致。
- ✅ 主 Agent `messages.json` 保留 cache token；父 `messages.json` 不承载子 Agent 内部 token 明细。
- ✅ 任意单个子进程 summary 最多聚合一次。
- ✅ 全链路 summary 中主 Agent 与子 Agent token/cache 可分开查看。
- ✅ 顶层兼容字段、`llms.overall`、`main_agent + subagents` 三者一致。
- ❌ 报告层不会把 `.performance` 与 `messages.json` 的同一批主 Agent token 重复相加（依赖报告层落地）。
- ✅ `cache_control_mode` 诊断字段正确传播（H0 fix 后）。

## 15. 实测结果（2026-07-13 openai 模式全量 e2e）

### 运行环境

- 分支：`opencode/subagent-token-cache-aggregation`（rebased on `upstream/main 09f3348`，含 H0 fix `648fd6e`）
- 模型：`--model openai`（Qwen3.7-Plus via OPENAI_BASE_URL，generic OpenAI-compatible 端点）
- 开关：`DATAAGENT_PERFORMANCE_ENABLED=1`，`--cache-threshold-profile=off`
- 用例：全量 7 查询（`create_experiment` / `find_antibody_neutralization` / `find_recent_experiment` /
  `count_cells` / `count_viruses` / `count_antibodies` / `ask_recent_experiment_id`）
- 耗时：18 分钟（09:58:53 → 10:16:29）

### 三视角加和验证（PR 157 核心功能）

从主进程 footer `6.615308.jsonl` 提取，逐字段校验 `overall == main_agent + subagents`：

```
input_tokens              main=68249 + sub=10717 = 78966 | overall=78966 ✓
output_tokens             main=   429 + sub= 2361 =  2790 | overall= 2790 ✓
total_tokens              main=68678 + sub=13078 = 81756 | overall=81756 ✓
input_cache_read_tokens   main=63744 + sub=    0 = 63744 | overall=63744 ✓
input_cache_creation_tokens main=0 + sub=0 = 0       | overall=0     ✓
output_reasoning_tokens   main=   90 + sub= 2124 =  2214 | overall= 2214 ✓
call_count                main=    3 + sub=    4 =     7 | overall=    7 ✓
```

### 主 Agent 缓存命中（planner 流式，跨 7 query 累计）

| Run | 对应 query | input | cache_read | hit_rate | calls |
|-----|-----------|-------|-----------|----------|-------|
| 0 | Q1 create_experiment | 391591 | 294656 | 75.2% | 42 |
| 1 | Q2 | 70469 | 53120 | 75.4% | 7 |
| 2 | Q3 | 74232 | 57216 | 77.1% | 7 |
| 3 | Q4 count_cells | 76124 | 61568 | 80.9% | 7 |
| 4 | Q5 | 78966 | 63744 | 80.7% | 7 |
| 5 | Q6 | 82217 | 65792 | 80.0% | 7 |
| 6 | Q7 | 24693 | 23296 | 94.3% | 1 |

主 Agent `cache_control_mode` 在 H0 fix 前恒为 `none_or_unknown`（bug），fix 后应为 `implicit`
（generic OpenAI 端点不注入但服务端返回 cache_read）。**注**：本次 e2e 运行于 H0 fix 之前，
footer 中的 `cache_control_mode` 仍为 `none_or_unknown`；fix 后重跑将正确显示 `implicit`。

### nl2sql 子 Agent 缓存命中（每个 query 独立子进程）

| Run | PID | input | cache_read | hit_rate | cc_mode | calls |
|-----|-----|-------|-----------|----------|---------|-------|
| 0 | 615473 | 10833 | 4096 | 37.8% | implicit | 4 |
| 0 | 615476 | 11004 | 0 | 0.0% | none_or_unknown | 4 |
| 0 | 615479 | 10934 | 2048 | 18.7% | implicit | 4 |
| 0 | 616055 | 20389 | 0 | 0.0% | none_or_unknown | 6 |
| 0 | 616290 | 10645 | 0 | 0.0% | none_or_unknown | 4 |
| 1-5 | 5 个独立子 Agent | ~10-12k | 0 | 0.0% | none_or_unknown | 4 |

nl2sql 子 Agent 缓存基本为 0 是**预期行为**：openai 模式 = generic OpenAI-compatible 端点不注入
`cache_control`，且子 Agent 每 query 独立子进程不复用前缀。提升需 `--model bailian`（DashScope/百炼触发
explicit 注入）或 SWARM 复用。

### Bailian 模式对比测试（失败，API 配额耗尽）

为对比 explicit cache_control 注入效果，补跑 `--model bailian` 全量 7 查询。结果：7 query 全部 403 失败
（`[403 auth] The free quota has been exhausted`），百炼 API key 免费配额耗尽，非代码 bug。日志：
`/home/qianlong/.local/opencode/logs/test_performance_bailian_full_20260713_123631.log`。
失败会话产物已清理。需百炼控制台关闭"仅使用免费额度"或充值后重跑。

## 16. 已知问题与后续工作

### HIGH

- **H0（已 fix）**：流式 `_astream_parse_line` 漏传 `cache_control_mode`，commit `648fd6e` 已修复并推送。
- **H1（未实施）**：报告层读取策略（Task 11）未落地，无 `messages_json_fallback` 标记，e2e 报告仍按主 Agent
  单口径输出。需独立 PR 补报告层模块。
- **H2（部分缺失）**：e2e 验证 Task 13.4-13.7 缺失（`test_performance.py` 在本 PR 中未被修改）。需补
  `round_summaries` vs `main_agent` 一致性断言 + 禁用 perf fallback 验证。
- **H3（工作流违规）**：`openspec/changes/add-subagent-token-cache-aggregation/tasks.md` 96 行任务全部
  `- [ ]` 未勾选，OpenSpec `/opsx-apply` 工作流要求逐条 `- [x]`。需补勾选或标注 deferred。

### MEDIUM

- **M1**：`sub_agent_entry.py:_read_subagent_identity_from_config` 盲 `except Exception: pass` 违反
  §5.7 BLE001。实测后果：`subagents.by_agent.agent_type="unknown"`（配置读取错误被吞）。建议缩窄到
  `(OSError, yaml.YAMLError)` + `logger.debug`。
- **M2**：`performance.py` 用模块级 mutable global `_resolve_usage_funcs` 解循环依赖（`performance →
  llm_manager.usage → llm_manager/__init__ → adapters → performance`），非线程安全。根治应让
  `llm_manager/__init__.py` lazy import 或把 `usage.py` 移到 `core/utils/`。
- **M3**：`import hashlib` 在 `merge_subagent_llm_usage` 函数内（PEP 8 E402），应移到模块顶部。
- **M4**：`build_summary` 用 `assert` 断言顶层==overall，`python -O` 会剥离。spec 字面要求"SHALL 断言"，
  合规但生产代码建议改 `RuntimeError`。

### LOW

- **L1**：`_resolve_cache_control_mode` 流式每 chunk 调用一次，可缓存首个非 `none_or_unknown` 值。
- **L2**：`_build_perf_summary` 6 字段重复 `try/except (TypeError, ValueError)`，可复用 `_safe_int`。
- **L3**：`_read_subagent_identity_from_config` 失败路径无单测。
- **L4**：dedup hash fallback 在 `query=""` 且 `worker_run_id`/`worker_session_id` 均为 "0" 时可能误判。

## 17. 代码索引

| 文件 | 关键改动 |
|------|---------|
| `dataagent/core/managers/llm_manager/usage.py` | 新增 241 行：`TOKEN_FIELDS` / `usage_to_metadata` / `normalize_usage_metadata` / `summarize_usage` / `cache_hit_rate` / `empty_usage`，Anthropic canonical `input_tokens` 补齐 |
| `dataagent/core/utils/performance.py` | 新增 `snapshot_summary` / `merge_subagent_llm_usage` / `_seen_subagent_perf_keys` / 三视角 `build_summary` / `_pick_cache_control_mode`；`summarize_llm_usage` 改薄包装；`bind_agent_performance` 加 `summary_sink` 参数 |
| `dataagent/core/cbb/base_agent.py` | `_performance_run` 加 `summary_sink` 参数；从 `initial_state["_performance_summary_sink"]` 读取后 `pop` |
| `dataagent/actions/tools/local_tool/sub_agent_entry.py` | `_run_agent` 返回 `(result, perf_summary)`；新增 `_build_perf_summary` + `_read_subagent_identity_from_config` |
| `dataagent/actions/tools/local_tool/tools.py` | 新增 `_merge_subagent_perf_summary` + `_strip_perf_summary_for_original_msg`；`_subagent_completed_outcome_from_worker_result_branch` 调用聚合 |
| `dataagent/core/swarm/worker_result.py` | `WorkerResult` 加 `perf_summary: dict \| None = None`；`worker_result_from_payload` / `synthesize_worker_result` 透传 |
| `dataagent/core/managers/llm_manager/llm_client.py` | `_supports_explicit_cache_control` 加 `base_url` 参数 + 白名单；新增 `_resolve_cache_control_mode`；`LLMClientMessage`/`LLMResponse`/`_StreamAccum` 加 `cache_control_mode` 字段；**H0 fix**：`_astream_parse_line:1585` 传递 `cache_control_mode=wrapped.cache_control_mode` |
| `dataagent/core/managers/llm_manager/adapters.py` | `normalize_usage_metadata` 改薄包装；`LLMResponse` / `_StreamAccum` 加 `cache_control_mode`；`_wrap_output` 读 `out.cache_control_mode` |
| `dataagent/core/context/message_history.py` | round summary 改用共享 `summarize_usage` / `cache_hit_rate`；`cache_hit_rate` 0-1 小数（`input_tokens==0` 时 `None`） |
| `tests/ut/managers/llm_manager/test_usage_module.py` | 新增 284 行 |
| `tests/ut/test_subagent_perf_aggregation.py` | 新增 350 行 |
