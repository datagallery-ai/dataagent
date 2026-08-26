---
hide:
  - navigation
---

## Notes

This page summarizes development, testing, and documentation maintenance. Capabilities are defined by the current code, example YAML files, and API chapters in this site.

## Development Guide

### Clone the Repository

```bash
git clone https://github.com/datagallery-ai/DataAgent.git
cd dataagent
```

### Install Dependencies

```bash
# Base dependencies
uv sync

# Test dependencies
uv sync --extra test

# Documentation dependencies
uv sync --extra mkdoc

# Development tools
uv sync --group dev
```

### Code Checks

```bash
uv run ruff check dataagent
uv run mypy dataagent
```

For formatting, use the formatting commands defined by the project toolchain.

### Build Documentation

DataAgent documentation site:

```bash
uv run mkdocs build -f docs/mkdocs.yml --strict
```

Local preview:

```bash
uv run mkdocs serve -f docs/mkdocs.yml
```

## Build Packages

During development you can use the source tree and `uv run` directly. To build distribution packages:

```bash
uv build
```

Output is written to `dist/`.

## Documentation Maintenance Principles

- Example paths must exist in the current repository.
- API signatures must match the current code.
- Do not document planned or legacy behavior as currently available.
- User-facing chapters should focus on how to configure, run, and troubleshoot.
