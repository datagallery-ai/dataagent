# 用 Trace 做评测

DataAgent 的 Trace 记录一次执行的步骤、耗时和可用的输入输出。可以把已结束的 span [实时发给接收端](trace-export.md)，也可以使用 [Home 里的 OTLP JSON](../../development/architecture/execution-traces.md#local-files) 评测历史执行。两条路径都需要定义评测项、输入输出字段和评分规则。接收到 span 后，平台可以展示调用链，评分则由你配置的评测器或脚本执行。

## 从本地文件提取样本

在后端或 SDK 的 Home 找到 `runtime/users/<user>/sessions/<thread>/traces/<native_run_id>.json`。用 `traceId` 和 `spanId` 标识样本，使用 `parentSpanId` 定位父步骤。整轮评测通常取根 span，诊断单次模型调用时可从子 span 中选择 `langsmith.span.kind=llm` 的记录。

下面用 Python 读取一个实际生成的文件，输出根 span 的标识、状态、耗时及已有的输入输出。先把 `/path/to/run.json` 换成本地文件路径。若产物包含敏感数据，先在本地审阅输出的字段和内容。

```bash
python3 - /path/to/run.json <<'PY'
import json
import sys
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

def structured_value(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value

for resource in data["resourceSpans"]:
    for scope in resource.get("scopeSpans", []):
        for span in scope.get("spans", []):
            if span.get("parentSpanId"):
                continue
            attrs = {item["key"]: item["value"] for item in span.get("attributes", [])}
            prompt = attrs.get("gen_ai.prompt", {}).get("stringValue")
            completion = attrs.get("gen_ai.completion", {}).get("stringValue")
            record = {
                "trace_id": span["traceId"],
                "span_id": span["spanId"],
                "name": span["name"],
                "status_code": span.get("status", {}).get("code", 0),
                "duration_ms": (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1_000_000,
                "input": structured_value(prompt),
                "output": structured_value(completion),
            }
            print(json.dumps(record, ensure_ascii=False))
PY
```

用输出的 JSON 行建立评测样本。根据实际 `input` 和 `output` 结构，提取用户问题和最终回答，再按测试集的案例 ID 补上预期结果或参考答案。以 [SQLite 教程](sqlite-tutorial.md) 为例，可用路由、执行状态和耗时分析轨迹，用生成的 CSV 核对三条汇总值。只看到 SQL 或工具执行成功不足以判断查询结果正确。测试集和评测结果可以单独保存，以 Trace ID 或 Span ID 回查运行。

Span 属性来自运行时的 LangChain/LangSmith 转换。输入、输出、模型名和 Token 用量都按实有值提取。中断、隐藏或缺失的字段保留为空。向平台映射字段时，先用一条真实输入确认问题、回答、模型调用与工具调用各落在哪个 span，再决定评测整轮还是某一步。涉及系统提示、历史、工具输出或业务数据时，按实际可外发范围筛选样本。详见 [Trace 的数据边界](../../development/architecture/execution-traces.md#trace-data-boundaries)。

## 接入评测平台

按[导出指南](trace-export.md#platform-receivers)将 Trace 送入平台后，对照一条实际收到的记录映射字段。模型、输入、输出和用量视图可能需要对应的 OpenInference、OpenTelemetry GenAI 或平台属性。

| 平台 | 评测与写回 |
| --- | --- |
| Arize Phoenix | 用 `client.spans.get_spans_dataframe` 取数，按实际属性映射评测列，用 `client.spans.log_span_annotations_dataframe` 按 Span ID 写回。 [轨迹评测](https://arize.com/docs/phoenix/tracing/how-to-tracing/feedback-and-annotations/evaluating-phoenix-traces) |
| Langfuse | 在 LLM-as-a-Judge 中选取输入、输出和上下文字段，或在自己的评测脚本中算分后按 Trace ID 写入 Score。评测单个观察项时同时提供 Observation ID。 [评分 API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk) |
| Opik | 配置 trace 或 span 级在线评测规则，选择字段并检查结果里的 feedback score。历史轨迹可在 Traces 页面选择现有轨迹后执行评测规则。 [评测规则](https://www.comet.com/docs/opik/production/online-evaluation/rules) |

写回评分时使用评测平台确实收到的 ID，或使用本地文件解析出的 ID 与实际导入记录对应。若同一运行同时实时发送和事后导入，以 `langsmith.metadata.dataagent_run_id` 等原生标识做关联。两份数据的 OTLP ID 和父子关系可能不同，不能用文件名推算实时导出的 Trace ID。

## 把历史 Trace 文件导入接收端

历史文件的导入命令、HTTP/JSON 支持与 Collector 转发见[导入历史 Trace](trace-export.md#import-json)。
