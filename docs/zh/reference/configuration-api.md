# 配置速查

首次部署时，在这里找到要改的文件和最小模型配置。

控制面没有配置 REST。网页上改不了模型或工作区。

## 要改的文件 { #files-to-edit }

| 文件 | 作用 |
| --- | --- |
| `<home>/config.yaml` | 个人配置。Home 默认是 `~/.dataagent` |
| `<home>/.env` | 模型名、地址和密钥 |
| 仓库根目录 `.env` | 从源码用 `npm run start` 或 `./deploy.sh` 启动时，启动器会读取它 |

用启动进程的 `DATAAGENT_HOME` 指定 Home。通过 `--config` 和 `--env-file` 可以分别选择 YAML 和环境文件。文件优先级见[环境变量参考](cli-and-environment.md#environment-variables)。

## 最少的模型

```yaml
schema_version: 4
models:
  primary:
    provider: openai
    name: $env{LLM_MODEL}
    base_url: $env{LLM_BASE_URL}
    api_key: $env{LLM_API_KEY}
```

在选中的环境文件里填入三个 `LLM_*` 的实际值。监听地址在同一份 YAML 的 `server` 段设置，默认值和其他选项见[监听配置](configuration.md#server-settings)。

改模型、端口或 Home 后要重启控制面。

## 只读目录

输入目录通过 YAML 的 `dataagent.input_workspaces` 配置，目录需已存在。步骤见 [数据从哪里来](../guides/inputs.md)。

## 更多配置

- [配置操作](../guides/configuration.md)介绍模型切换、运行限额、产物和插件配置。
- [配置字段](configuration.md)列出类型、默认值和合并规则。
- [命令与环境变量](cli-and-environment.md)列出启动参数和环境变量。
