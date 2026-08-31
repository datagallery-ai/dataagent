# Documentation source

This directory contains the public documentation source for the DataFoundry GitHub Pages site. The site uses MkDocs Material.

- Site home: [index.md](index.md)
- English: [English docs](en/index.md)
- 中文：[中文文档](zh/index.md)

## Quick links

| Goal | English | 中文 |
| --- | --- | --- |
| Product positioning | [Overview](en/overview.md) | [产品概览](zh/overview.md) |
| Local demo | [Quick start](en/quick-start.md) | [快速开始](zh/quick-start.md) |
| Capability scope | [Capabilities](en/capabilities.md) | [能力全览](zh/capabilities.md) |
| Web workbench | [Web workbench guide](en/guides/web-workbench.md) | [Web 工作台指南](zh/guides/web-workbench.md) |
| TUI | [TUI guide](en/guides/tui.md) | [TUI 指南](zh/guides/tui.md) |
| Data sources | [Data sources guide](en/guides/data-sources.md) | [数据源指南](zh/guides/data-sources.md) |
| Supported sources | [Supported data sources](en/reference/supported-datasources.md) | [支持的数据源](zh/reference/supported-datasources.md) |
| REST & config API | [REST API](en/reference/rest-api.md), [Configuration API](en/reference/configuration-api.md) | [REST API](zh/reference/rest-api.md), [配置 API](zh/reference/configuration-api.md) |
| Agent runtime | [Agent Runtime reference](en/reference/agent-runtime.md) | [Agent Runtime 参考](zh/reference/agent-runtime.md) |
| Architecture | [Architecture overview](en/architecture/overview.md) | [架构概览](zh/architecture/overview.md) |
| Security | [Security](en/security.md) | [安全说明](zh/security.md) |

## Documentation scope

`docs/` contains public documentation and assets suitable for open-source visitors. Historical iteration notes, implementation plans, PRDs, research material, and source-sensitive content do not belong in the public docs path.

Engineering contracts, historical design notes, and internal documentation with ongoing maintenance value but not suitable for public reading stay outside this repository. When content should be public, sanitize it for users or contributors first, then add it under `docs/`.

## Maintenance rules

- Public docs must not link to internal documentation areas.
- When adding external-facing capabilities, update both `en/` and `zh/` documentation.
- Example credentials must use placeholder values such as `replace-with-your-key`.
- Do not retain source-sensitive narrative.
- After doc changes, run `npm run docs:build` and `npm run smoke:docs`.

## Local preview and GitHub Pages

Local preview:

```bash
python -m venv .venv-docs
source .venv-docs/bin/activate
python -m pip install -r requirements-docs.txt
npm run docs:dev
npm run docs:build
```

After pushing to `main`, `.github/workflows/docs.yml` publishes the site to GitHub Pages:

`https://datagallery-lab.github.io/datafoundry/`

For private repositories, enable GitHub Pages first (Settings → Pages → Source: **GitHub Actions**), confirm your GitHub plan allows Pages on private repos, and control public visibility in Pages settings.
