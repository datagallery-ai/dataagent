---
hide:
  - navigation
---

## 说明

本页汇总开发、测试和文档维护相关说明。功能能力以当前代码、示例 YAML 和各章节接口说明为准。

## 开发指南

### 克隆仓库

```bash
git clone https://github.com/datagallery-ai/DataAgent.git
cd dataagent
```

### 安装依赖

```bash
# 基础依赖
uv sync

# 测试依赖
uv sync --extra test

# 文档依赖
uv sync --extra mkdoc

# 开发工具
uv sync --group dev
```

### 代码检查

```bash
uv run ruff check dataagent
uv run mypy dataagent
```

如需格式化，按项目当前工具链执行对应格式化命令。

### 构建文档

DataAgent 文档站点：

```bash
uv run mkdocs build -f docs/mkdocs.yml --strict
```

本地预览：

```bash
uv run mkdocs serve -f docs/mkdocs.yml
```

## 编译出包

开发阶段通常可以直接使用源码和 `uv run`。需要构建分发包时执行：

```bash
uv build
```

生成产物位于 `dist/`。

## 文档维护原则

- 示例路径必须能在当前仓库中找到。
- API 签名以当前代码为准。
- 不把规划中或历史实现写成当前可用能力。
- 面向用户的章节优先说明“怎么配置、怎么运行、怎么排查”。
