---
hide:
  - navigation
  - toc
  - edit
---

<div class="df-home" markdown>

<section class="df-home-hero" markdown>

# DataFoundry

企业级 Data Agent 工作台 —— 用统一语义读懂业务口径，在只读安全边界内执行多表、多步的复杂分析，每一步可审计、可回放，把一句提问变成一次可信的数据分析。

</section>

<figure class="df-home-shot" markdown>
![DataFoundry Web 工作台](../assets/readme/gui-demo.gif){ .df-shot }
<figcaption>Web 工作台、终端工作流和后端运行时共享同一套受控任务模型。</figcaption>
</figure>

<section class="df-proof-grid" markdown>

<div class="df-proof" markdown>
### 语义约束下的准确性
先解析 schema、指标口径、字段关系和数据源上下文，再生成 SQL，减少猜表、猜字段、错关联和口径漂移。
</div>

<div class="df-proof" markdown>
### 受控的数据边界
默认只读 SQL、凭据隔离、字段脱敏、行数限制、超时和审计，比通用 coding agent 更适合真实企业数据环境。
</div>

<div class="df-proof" markdown>
### 数据任务运行时
复杂分析通过 schema 检查、工具策略、事件流、Artifacts 和可回放历史逐步收敛，并贯通 Web、TUI 与 API。
</div>

</section>

<section class="df-runtime-preview" markdown>

<div class="df-runtime-copy" markdown>
## 一套运行时，三种入口

Web 工作台适合可视化复盘，TUI 适合 SSH 和终端工作流，API / AG-UI 适合把同一套受控数据任务模型嵌入自己的产品。

[TUI 指南](guides/tui.md) · [观看终端演示](../assets/readme/tui-demo.gif)
</div>

<a class="df-tui-preview" href="../../assets/readme/tui-demo.gif" aria-label="观看 DataFoundry TUI 演示">
  <img src="../../assets/readme/tui-demo.gif" alt="DataFoundry TUI 演示" loading="lazy">
</a>

</section>

## 选择合适的开始路径

<div class="df-grid df-route-grid" markdown>

<div class="df-card" markdown>
### 本地试用
启动工作台，并基于内置 DTC Growth Review 完成第一次数据分析。

[开始快速体验](quick-start.md){ .df-card-action }
</div>

<div class="df-card" markdown>
### 理解系统
了解运行时、安全边界和 Data Gateway 架构。

[查看架构概览](architecture/overview.md){ .df-card-action }
</div>

<div class="df-card" markdown>
### 接入 API
使用 REST 配置 API 与 Agent Runtime / AG-UI 入口。

[查看 API 参考](reference/rest-api.md){ .df-card-action }
</div>

</div>

</div>
