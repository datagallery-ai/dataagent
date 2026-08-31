# 用户指南

DataFoundry 是面向数据分析场景的 AI 工作台，把自然语言提问、数据源管理、只读 SQL 执行、分析追溯和结果产出放在同一个工作流里。

<div class="df-grid" markdown>

<div class="df-card" markdown>
<p class="df-card-title">产品概览</p>
了解 DataFoundry 的定位、适用场景和能力边界。

[阅读概览](overview.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">快速开始</p>
启动本地工作台，并用内置 demo 数据源跑通第一次分析。

[本地启动](quick-start.md)
</div>

<div class="df-card" markdown>
<p class="df-card-title">DTC 增长案例</p>
用真实感增长场景理解 schema 检查、只读 SQL 和可追溯经营结论。

[查看案例](examples/dtc-growth-demo.md)
</div>

</div>

## 推荐体验路径

1. 阅读 [产品概览](overview.md)，确认 DataFoundry 解决的问题和能力边界。
2. 按 [快速开始](quick-start.md)：Ubuntu / Debian 推荐 `./deploy.sh`；Windows / macOS 及其他环境用手动 npm（`npm install` → 配置 → `build` / `start`）。登录 Web 后创建模型 Profile，再用内置 DTC Growth Review 跑通第一个问题。
3. 查看 [能力全览](capabilities.md)，了解 Web 工作台、TUI 和后端 API 的能力覆盖。
4. 根据使用入口选择 [Web 工作台指南](guides/web-workbench.md) 或 [TUI 指南](guides/tui.md)。
5. 需要接入自有数据时，再阅读 [数据源指南](guides/data-sources.md)。

## 文档边界

这套文档面向产品试用、客户演示、开源访客和集成开发者。如果你是二次开发或集成方，请优先阅读 `API Reference` 和 `Developer Guide`。
