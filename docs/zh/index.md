# 用户指南

这是一个本地控制面，Web 和 TUI 是它的两种展示。

<div class="df-grid" markdown>

<div class="df-card" markdown>
<p class="df-card-title">产品概览</p>
了解 DataAgent 的定位、适用场景和能力边界。

[阅读概览](overview.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">快速开始</p>
写入模型密钥，在网页或 TUI 里完成第一次提问。

[本地启动](quick-start.md)
</div>

</div>

## 推荐体验路径

1. 阅读 [产品概览](overview.md)，确认 DataAgent 解决的问题和能力边界。
2. 按 [快速开始](quick-start.md)：Linux（包括 openEuler）推荐 `./deploy.sh`；Windows / macOS 可用手动 npm（`npm install` → 配置 → `build` / `start`）。把模型密钥写入 `.env`，打开 `/data-tasks` 或 TUI 提问。
3. 查看 [能力全览](capabilities.md)，了解 Web、TUI 和控制面实际提供的接口。
4. 根据使用入口选择 [Web 工作台指南](guides/web-workbench.md) 或 [TUI 指南](guides/tui.md)。
5. 需要让模型读本机目录时，阅读 [数据从哪里来](guides/data-sources.md)。

## 文档边界

这套文档面向产品试用、客户演示、开源访客和集成开发者。如果你是二次开发或集成方，请优先阅读 `API Reference` 和 `Developer Guide`。
