# Documentation source

This directory in the [DataAgent repository on GitCode](https://gitcode.com/datagallery/dataagent) is the source for the [DataGallery online documentation](https://datagallery.cn/docs). MkDocs Material provides a local preview. For release artifacts and their source versions, see [package deployment](en/guides/deploy-packages.md#get-artifacts).

- Site home in [English](index.md) and [Chinese](zh/home.md)
- Use and integration in [English](en/index.md) and [Chinese](zh/index.md)
- [Developer Docs](development/index.md)
- [Community](en/community/index.md) and [社区](zh/community/index.md)

Maintain routes, response envelopes, and HTTP status codes in the bilingual REST references. Keep the AG-UI request and event contract in `docs/zh/reference/events.md` and the A2A contract in `docs/zh/reference/a2a.md`. Integration and architecture pages link to those contracts.

## Documentation scope

The DataGallery website renders both entry points from the same source tree. Use and integration documents have matching source paths in `en/` and `zh/`. Developer setup, architecture, testing, and release documentation live in `development/`.

## Maintenance rules

- Guides own executable tasks. Reference pages own public contracts, field constraints, defaults, and errors. Development pages own runtime implementation and verification.
- Keep the old page and section targets when splitting or consolidating published content. Link readers to the section that now owns the explanation.

- Product and Python technical guides share this documentation tree; link to the relevant guide rather than duplicating it.
- When adding or changing an external-facing page, update its counterpart in the other language, including examples and referenced section anchors.
- Document behavior that readers can use in the current checkout. Consult Git tags and history for earlier versions.
- When moving a published page, check its output URL. The [MkDocs hook](../scripts/dev/docs_hooks.py) preserves established URLs for migrated pages.
- Example credentials must use placeholder values such as `replace-with-your-key`.
- Do not retain source-sensitive narrative.
- After doc changes, run `npm run docs:build` and `npm run smoke:docs`.

## Local preview

See [local preview](development/setup.md#preview-docs) for the MkDocs environment and commands.
