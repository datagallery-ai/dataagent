# HTTP 接口

控制面的现行 HTTP 契约见 [REST API 参考](../../zh/reference/rest-api.md)或 [REST API reference](../../en/reference/rest-api.md)。

| 查阅内容 | 入口 |
| --- | --- |
| <a id="get-healthz"></a><a id="get-ready"></a><a id="apiv1"></a>健康、身份和能力 | [健康与身份](../../zh/reference/rest-api.md#health-and-identity) |
| <a id="get-apiv1sessions"></a><a id="get-apiv1sessionsthreadid"></a>查询会话 | [列出会话](../../zh/reference/rest-api.md#list-sessions)、[读取会话](../../zh/reference/rest-api.md#session-detail) |
| <a id="post-apiv1runsrunidcancel"></a>取消运行 | [取消运行](../../zh/reference/rest-api.md#cancel-run) |
| <a id="post-apiv1runs"></a>对话请求、人工反馈与事件 | [AG-UI 参考](../../zh/reference/events.md) |
| <a id="get-apiv1sessionsthreadidoutputs"></a><a id="get-apiv1sessionsthreadidoutputspreview"></a>文件列表与预览 | [列出文件](../../zh/reference/rest-api.md#list-outputs)、[预览文本](../../zh/reference/rest-api.md#preview-output) |
| <a id="_1"></a>错误处理 | [HTTP 错误](../../zh/reference/rest-api.md#http-errors) |
| <a id="a2a"></a>A2A 发现和任务 | [A2A 参考](../../zh/reference/a2a.md) |

## 实现和验证入口

- 健康、身份、能力与取消路由在 [product.py](../../../dataagent/server/product.py)，JSON 信封及错误转换在 [envelopes.py](../../../dataagent/server/envelopes.py)。
- 会话、REST 运行和事件流在 [app.py](../../../dataagent/server/app.py)。文件列表和预览在 [outputs.py](../../../dataagent/server/outputs.py)。
- 接口验证见 [产品路由](../../../tests/test_product_api.py)、[会话与运行](../../../tests/test_api.py)、[人工反馈](../../../tests/test_api_hitl.py)及[产物](../../../tests/test_outputs.py)测试。
