# DataLink

`dataagent serve` 不挂 DataLink 路由，也不会在部署时启动 `components/datalink`。

网页上的 DataLink 面板如果去请求 `/api/v1/datalink/*`，控制面返回 404。需要这套独立服务时，按 `components/datalink` 自己的说明单独运行；它不是当前控制面的一部分。

模型要访问的本机文件，用 [数据从哪里来](data-sources.md) 里的工作区目录。
