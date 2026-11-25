# DataAgent UI

Web 在 `web/`，TUI 在 `tui/`，运行期共享代码在 `shared/`。在仓库根目录执行 `npm install --include=dev`、`npm run build` 和 `npm run start:tui`；需要 TUI 自行启动源码后端时，先用 `uv sync` 准备 Python 环境。

`@datagallery/dataagent-ui` 是统一 UI 包的工程边界。当前保留 `private: true`；统一 `dataagent-ui` 命令、`launcher/` 和可独立安装的 npm 产物在后续出包阶段实现。不要将当前源码目录直接作为发行成品。

共享代码不导入仓库 `scripts/` 或 Python 源码。开发环境同步与部署配置写入留在 `scripts/`；TUI 当前的源码启动方式仍使用仓库根目录 `.venv`，后续启动器再增加已安装后端的解析。
