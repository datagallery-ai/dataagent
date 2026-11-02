# 数据从哪里来

这篇只讲试用时怎么把本机目录交给模型。输入种类的对照表在 [支持的输入](../reference/supported-datasources.md)。

控制面没有数据源注册接口。网页和 TUI 都不能创建、测试或切换一个“数据源”资源。

## 只读目录

要让模型读本机文件，把目录写进工作区。目录必须在启动前已经存在。

```json
{
  "dataagent": {
    "workspaces": [
      {"name": "input", "path": "/data/reports", "read_only": true}
    ]
  }
}
```

也可以在启动控制面时重复传入 `--workspace`，或设置 `DATAAGENT_WORKSPACE`。这三项里，命令行优先。

`read_only` 只能是 `true`。新会话把这份列表写入 `sessions.sqlite` 的 `session_bindings`，之后不改。Agent 新写的文件在该会话的产物目录，默认是 `<home>/outputs/<user>/<thread>/`，不在工作区里。

系统提示要求把工作区当只读。这不改变目录的操作系统权限。

## 插件

Skill、插件和 MCP 也是文件，放在 Home 里，而不是通过 REST 注册。见 [配置](../reference/configuration-api.md)。

针对数据库的问答不是控制面自带的。只有启用了相应插件，并写好该插件的 `plugins.config` 之后，那条能力才存在。
