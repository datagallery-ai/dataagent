# Documentation source

This directory contains the public documentation source for the DataAgent GitHub Pages site. The site uses MkDocs Material.

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
| Local files | [Where data comes from](en/guides/data-sources.md) | [数据从哪里来](zh/guides/data-sources.md) |
| Supported inputs | [Supported inputs](en/reference/supported-datasources.md) | [支持的输入](zh/reference/supported-datasources.md) |
| REST and configuration | [REST API](en/reference/rest-api.md), [Configuration](en/reference/configuration-api.md) | [REST API](zh/reference/rest-api.md), [配置](zh/reference/configuration-api.md) |
| Agent runtime | [Agent Runtime reference](en/reference/agent-runtime.md) | [Agent Runtime 参考](zh/reference/agent-runtime.md) |
| Architecture | [Architecture overview](en/architecture/overview.md) | [架构概览](zh/architecture/overview.md) |
| Security | [Security](en/security.md) | [安全说明](zh/security.md) |

## Two documentation trees

| Tree | Reader | Put here | Leave out |
| --- | --- | --- | --- |
| `docs/` | Someone deploying or trying Web and TUI, or calling the HTTP API | Product shape, deploy and first question, what each UI can do, the external request and response contract, local security expectations | Plugin schemas, SDK lifecycle, hook order, bootstrap steps, file modes, `dataagent/core` maintenance |
| `runtime/agentkit/docs/` | Someone changing configuration fields, extensions, or the runtime | Field table, Home layout, SDK, plugins, HTTP details used to change `serve/`, architecture and tests | `deploy.sh` lifecycle, bilingual product tour, Web page layout |

The public configuration page keeps only the files a deployer edits and the minimum model. The field table is `runtime/agentkit/docs/reference/configuration.md`.

When a public page and a runtime page both describe an HTTP path or a deployer-facing default, change them together. They must not disagree.

## Documentation scope

`docs/` is the GitHub Pages site. Historical plans, PRDs, and source-sensitive notes do not belong here.

## Maintenance rules

- Public docs must not link into `runtime/agentkit/docs/`.
- When adding an external-facing capability, update both `en/` and `zh/`.
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
