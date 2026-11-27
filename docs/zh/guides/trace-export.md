# 导出 Trace 到 OpenTelemetry

DataAgent 默认在每轮对话结束后保存本地 OTLP JSON。配置接收地址后，DataAgent 还会在各 Span 结束时通过 OTLP/HTTP 发送 Protobuf 数据。本地 Trace 仍会写入。此出口传输运行 Trace，日志继续写入本地日志文件。

如果已有接收 OTLP/HTTP 的 Collector 或观测平台，直接[配置 DataAgent](#configure-dataagent)。要在本机确认链路，可以先启动一个 Collector。

<a name="local-collector"></a>

## 启动本机 Collector

创建 `otel-collector.yaml`。这份配置只开启 HTTP 接收和 Trace 管道，接收到的 Span 会打印在 Collector 输出中。用合成问题验证，因为详细输出会包含 Span 属性中的业务内容。

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
```

在同一目录运行 Collector。端口只发布到本机回环地址。

```bash
docker run --rm --name dataagent-otel-collector \
  -p 127.0.0.1:4318:4318 \
  -v "$PWD/otel-collector.yaml:/etc/otelcol/config.yaml:ro" \
  otel/opentelemetry-collector:0.166.0
```

Collector 的安装和部署选项见 [OpenTelemetry 官方指南](https://opentelemetry.io/docs/collector/install/docker/)。

<a name="configure-dataagent"></a>

## 配置 DataAgent

在**启动 DataAgent 后端的终端**中设置完整的 Trace 接收 URL，再启动或重启后端。下例适用于 DataAgent 与上面的 Collector 在同一台宿主机上运行。

```bash
export DATAAGENT_OTLP_TRACES_ENDPOINT='http://127.0.0.1:4318/v1/traces'
dataagent serve
```

使用源码启动时，变量也可以写入由 `npm run start` 或 `./deploy.sh` 读取的仓库根目录 `.env`。直接运行 Server 时，请在进程环境中设置。后端读取的 Home `.env` 和 `dataagent serve --env-file` 不会将这些值写入进程环境。Web 或 TUI 连接已有 Server 时，在那个 Server 上配置。环境范围见[命令与环境变量](../reference/cli-and-environment.md)。

DataAgent 原样使用指定 URL，因此要传 `/v1/traces` 或接收方规定的完整路径。端口 `4318` 是 Collector 常用的 HTTP 端口，`4317` 通常用于 gRPC。设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 或 `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` 不会启用 DataAgent 导出。

接收方要求认证时，用 `DATAAGENT_OTLP_HEADERS` 提供一个或多个逗号分隔的 `key=value` 请求头。下面是需要 Bearer token 的接收端示例。

```bash
export DATAAGENT_OTLP_HEADERS='authorization=Bearer replace-with-your-token'
```

<a name="verify"></a>

## 验证和排查

1. 发送一条合成问题，等待回复结束。在 Collector 输出或接收平台里查找资源属性 `service.name=dataagent`，确认收到这一轮的 Span。
2. 在 `<home>/runtime/users/<user>/sessions/<thread>/traces/` 找到该轮的本地 JSON。用 Span 属性 `langsmith.metadata.dataagent_trace_id` 对照两端。文件会按原生运行 ID 重整 Trace ID 和父子关系，接收端的原始 OTLP ID 可能不同。
3. 若只有本地文件，检查后端进程是否实际收到了变量，以及接收地址、协议、认证和网络是否正确。后端日志中的 `OTLP live export enabled` 表示导出器初始化成功，收到 Span 才能确认网络链路。若看到 `OTLP_EXPORTER_INIT_ERROR`，先检查导出器配置。请求头格式错误时，按逗号分隔的 `key=value` 修正后重新运行。

<a name="platform-receivers"></a>

## 选择评测平台的接收地址

| 平台 | 接收与核对 |
| --- | --- |
| Arize Phoenix | 自托管常用 `http://127.0.0.1:6006/v1/traces`，使用 HTTP Protobuf。开启认证时按部署配置提供 `authorization`，可通过 `x-project-name` 指定接收项目。 [官方接入说明](https://arize.com/docs/phoenix/deployment/authentication) |
| Langfuse | 对应区域或自托管地址的 `/api/public/otel/v1/traces`，支持 HTTP Protobuf 和 OTLP JSON。使用项目公钥与密钥的 Basic 认证。使用 v4 数据路径时按平台接入说明设置 `x-langfuse-ingestion-version: 4`。 [官方接入说明](https://langfuse.com/integrations/native/opentelemetry) |
| Opik | 使用实例的完整 HTTP traces 地址。云端示例为 `https://www.comet.com/opik/api/v1/private/otel/v1/traces`，按实例提供 `Authorization`、`projectName`、`Comet-Workspace`。在项目中检查根 span 和映射出的输入输出。 [官方接入说明](https://www.comet.com/docs/opik/integrations/opentelemetry) |

例如，已把 Langfuse 项目公钥与密钥加载到环境时，使用下列配置向启用 v4 接入路径的云端项目发送数据。其他区域或自托管环境请换成项目的实际域名。

```bash
langfuse_basic=$(printf '%s:%s' "$LANGFUSE_PUBLIC_KEY" "$LANGFUSE_SECRET_KEY" | base64 | tr -d '\r\n')
export DATAAGENT_OTLP_TRACES_ENDPOINT='https://cloud.langfuse.com/api/public/otel/v1/traces'
export DATAAGENT_OTLP_HEADERS="Authorization=Basic ${langfuse_basic},x-langfuse-ingestion-version=4"
```

<a name="import-json"></a>

## 把历史 Trace 文件导入接收端

要保留原始 span、父子关系和时间戳，可把文件的完整内容发送给支持 **OTLP/HTTP JSON** 的接收端。以下使用 [已启用 OTLP HTTP 的 Collector](#local-collector)，可以在测试环境转发到只接收 Protobuf 的平台。

```bash
curl --fail-with-body --silent --show-error \
  -H 'Content-Type: application/json' \
  --data-binary '@/path/to/run.json' \
  http://127.0.0.1:4318/v1/traces
```

确认响应没有拒收的 span，并按文件中的 `traceId` 和原始时间范围在下游查找。Collector 的接收成功仅确认入口收到了数据，还要核对转发或下游存储。对于只支持二进制 Protobuf 的端点（例如 Phoenix 的 HTTP 接收接口），使用 Collector 转发或做格式转换。[Collector 接收配置](https://github.com/open-telemetry/opentelemetry-collector/blob/main/receiver/otlpreceiver/README.md#writing-with-httpjson) · [OTLP JSON 规范](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding) · [Phoenix 接收接口](https://github.com/Arize-ai/phoenix/blob/main/src/phoenix/server/api/routers/v1/traces.py)

Langfuse 支持直接导入 OTLP JSON。将示例中的接收 URL 换成项目地址的 `/api/public/otel/v1/traces`，按上面的平台表格添加 Basic 认证及适用的接入版本头。对于 Opik，可用下面的 Collector 转发方式，将接收地址和请求头替换为 Opik 的配置。 [Langfuse JSON 导入示例](https://langfuse.com/docs/observability/get-started)

以 Phoenix 为例，在已有 Collector 配置的 `exporters` 下加入 `otlp_http/phoenix`，并把它加入 `service.pipelines.traces.exporters`。从 Collector 所在网络访问下述地址。开启认证时按接收端要求在该 exporter 下配置请求头。

```yaml
exporters:
  otlp_http/phoenix:
    traces_endpoint: https://phoenix.example.com/v1/traces
    encoding: proto
service:
  pipelines:
    traces:
      exporters: [debug, otlp_http/phoenix]
```

[Collector HTTP 导出配置](https://github.com/open-telemetry/opentelemetry-collector/blob/main/exporter/otlphttpexporter/README.md)

导入前确认接收项目、认证和数据保留策略。先用一条无敏感信息的轨迹验证完整链路。批量导入按文件及 `traceId`、`spanId` 去重，尤其避免把同一批已实时发送的执行再作为一组新的评测样本。导入只发送已记录的轨迹，不会重新执行 Agent 或工具。

要提取样本、映射字段或保存分数，见[用 Trace 做评测](trace-evaluation.md)。
