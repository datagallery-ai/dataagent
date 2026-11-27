# Documentation source

This directory contains the public documentation source for the DataAgent GitHub Pages site. The site uses MkDocs Material.

- Site home in [English](index.md) and [Chinese](zh/home.md)
- Use and integration in [English](en/index.md) and [Chinese](zh/index.md)
- [Developer Docs](development/index.md)
- [Community](en/community/index.md) and [社区](zh/community/index.md)

Maintain routes, response envelopes, and HTTP status codes in the bilingual REST references. Keep the AG-UI request and event contract in `docs/zh/reference/events.md` and the A2A contract in `docs/zh/reference/a2a.md`. Integration and architecture pages link to those contracts.

## Documentation scope

The MkDocs build publishes both entry points on one site. Use and integration documents have matching source paths in `en/` and `zh/`. Developer setup, architecture, testing, and release documentation live in `development/`.

## Maintenance rules

- Product and Python technical guides share this documentation tree; link to the relevant guide rather than duplicating it.
- When adding or changing an external-facing page, update its counterpart in the other language, including examples and referenced section anchors.
- Document behavior that readers can use in the current checkout. Consult Git tags and history for earlier versions.
- When moving a published page, check its output URL. The [MkDocs hook](../scripts/dev/docs_hooks.py) preserves established URLs for migrated pages.
- Example credentials must use placeholder values such as `replace-with-your-key`.
- Do not retain source-sensitive narrative.
- After doc changes, run `npm run docs:build` and `npm run smoke:docs`.

## Local preview and GitHub Pages

See [local preview](development/setup.md#preview-docs) for the MkDocs environment and commands.

After pushing to `main`, `.github/workflows/docs.yml` publishes the site to GitHub Pages:

`https://datagallery-lab.github.io/datafoundry/`

For private repositories, enable GitHub Pages first (Settings → Pages → Source: **GitHub Actions**), confirm your GitHub plan allows Pages on private repos, and control public visibility in Pages settings.
