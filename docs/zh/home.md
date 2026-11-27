---
hide:
  - navigation
  - toc
  - edit
---

<div class="df-home" markdown>

<section class="df-home-hero" markdown>

# DataGallery

本地 Data Agent 工作台。通过 Web 或 TUI 提问，结合已配置的插件与语义服务完成多步分析，并查看会话历史、产物和运行轨迹。

</section>

<figure class="df-home-shot" markdown>
![DataGallery Web 工作台](../assets/readme/gui-demo.gif){ .df-shot }
<figcaption>Web 工作台、终端工作流和后端运行时共享同一套受控任务模型。</figcaption>
</figure>

<section class="df-proof-grid" markdown>

<div class="df-proof" markdown>
### 语义约束下的准确性
先解析 schema、指标口径、字段关系和数据源上下文，再生成 SQL，减少猜表、猜字段、错关联和口径漂移。
</div>

<div class="df-proof" markdown>
### 数据访问与执行边界
输入工作区向 Agent 提供只读约定。文件工具和 shell 使用 Server 进程的操作系统权限。NL2SQL 的安全规则由插件配置决定。参见[安全说明](concepts/security.md)。
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

<a class="df-tui-preview" href="../../assets/readme/tui-demo.gif" aria-label="观看 DataGallery TUI 演示">
  <img src="../../assets/readme/tui-demo.gif" alt="DataGallery TUI 演示" loading="lazy">
</a>

</section>

## 选择合适的开始路径

<div class="df-grid df-route-grid" markdown>

<div class="df-card" markdown>
### 使用与集成
部署 DataAgent，使用 Web 或 TUI，或者通过 API、Python SDK 和扩展接入自己的系统。

[查看使用与集成文档](index.md){ .df-card-action }
</div>

<div class="df-card" markdown>
### 开发者文档
搭建源码环境，了解架构，运行测试和构建发布制品。

[查看开发者文档](../development/index.md){ .df-card-action }
</div>

</div>

</div>
