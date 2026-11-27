# 数据从哪里来

通过工作区给 DataAgent 提供本机目录，或按需配置扩展来接入其他输入。

<a name="supported-inputs"></a>

## 支持的输入

| 输入 | 怎么配置 |
| --- | --- |
| 本机目录 | 所选 YAML 的 `dataagent.input_workspaces` |
| Skill | `<home>/skills/<name>/SKILL.md` |
| 插件 | `<home>/plugins/<id>/` 或 `plugins.paths`，再写入 `plugins.enabled` |
| MCP | Home 或已启用插件根目录的 `.mcp.yaml` |
| 语义服务 | `config.yaml` 的 `semantic`。未配置时不创建客户端 |

查询数据库、数仓或表格文件需要启用相应插件，并在该插件的配置中声明数据。控制面没有数据源注册表，也不提供 `GET /api/v1/datasource-types`。网页和 TUI 按服务端配置访问数据。

<a name="read-only-workspaces"></a>

## 只读工作区

要让模型读取本机文件，把目录写进所选 YAML。目录必须已存在，并且服务进程可以读取和进入。

```yaml
dataagent:
  input_workspaces:
  - name: input
    path: /data/reports
    read_only: true
```

启动 Server 时可以用 `--config` 选择 YAML。相对路径按该 YAML 所在目录解析。工作区名称是逻辑标签，必须唯一。`read_only` 只接受 `true`。

修改列表后，重启服务并新建会话以使用新输入。已有会话恢复时仍沿用创建时的列表。生成文件写到会话的产物目录，默认是 `<home>/outputs/<user>/<thread>/`。产物与恢复规则见[会话与产物](sessions-and-outputs.md)。

系统提示要求模型把工作区当作只读。文件工具与 shell 仍使用服务进程已有的系统权限。详见[安全说明](../concepts/security.md)。
