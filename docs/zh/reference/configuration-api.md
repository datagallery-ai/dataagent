# 配置

这篇给部署和试用的人。它只说明要改哪些文件、最少写什么。字段全表、合并规则和插件参数不在本站展开。

控制面没有配置 REST。网页上改不了模型或工作区。

## 要改的文件

| 文件 | 作用 |
| --- | --- |
| `<home>/config.json` | 个人配置。Home 默认是 `~/.dataagent` |
| `<home>/.env` | 模型名、地址和密钥 |
| 仓库根目录 `.env` | `npm run start` 和 `./deploy.sh` 把它载入进程环境。进程里的非空值盖过这次选中的环境文件。没传 `--env-file` 时选中 Home `.env`，`--config` 不改变它 |

`DATAAGENT_HOME` 只认进程环境。启动目录里的 `config.json`，以及当前目录下的 `.dataagent/`，都不会被读取。

## 最少的模型

```json
{
  "schema_version": 4,
  "models": {
    "primary": {
      "provider": "openai",
      "name": "$env{LLM_MODEL}",
      "base_url": "$env{LLM_BASE_URL}",
      "api_key": "$env{LLM_API_KEY}"
    }
  }
}
```

`provider` 使用 `openai`。兼容服务也用这个值。监听地址在同一份文件的 `server.host` 和 `server.port`，默认 `127.0.0.1` 和 `8790`。网页和 TUI 都读这两个字段。

改模型、端口或 Home 后要重启控制面。

## 只读目录

工作区不是 Home 里的子目录，也没有配置接口。把已经存在的目录写进 `dataagent.workspaces`，或在启动时传 `--workspace`。步骤见 [数据从哪里来](../guides/data-sources.md)。
