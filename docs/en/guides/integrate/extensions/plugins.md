# Plugins

A plugin is a directory with `.plugin.yaml` at its root. Its directory name, manifest `id`, and entry in `plugins.enabled` must match.

Minimal manifest:

```yaml
schema_version: 3
id: common
tools:
  summarize_numbers:
    entrypoint: tools/statistics.py:summarize_numbers
```

See the [extension schema](../../../reference/extension-schema.md) and the runnable [common plugin](../../../../../examples/plugins/common/README.md).

## Install and enable

1. Copy it to `<home>/plugins/<id>/` for automatic discovery, or list the individual plugin directory in `plugins.paths`. Listing its parent does not scan all its children.
2. Add its ID in `<home>/config.yaml`:

```yaml
plugins:
  enabled:
  - common
```

Paths supply candidates. The loader compiles the enabled IDs and their transitive dependencies. IDs must be unique. `user` and `cli` are reserved.

Candidates come from packaged `dataagent/plugins`, Home `plugins/`, and `plugins.paths`. A real directory is counted once. Two distinct directories with the same enabled ID cause startup to fail.

Manifest `requires` names dependencies. The host resolves them among those candidates and loads dependencies first, once per ID. Missing, duplicate, or cyclic dependencies fail at startup. The loader does not install from the network or add Python import paths across plugins.

```yaml
schema_version: 3
id: de-agent
requires:
- nl2sql
defaults_file: config.yaml
```

A `defaults_file` can provide `plugins.config` defaults for the plugin and declared dependencies. Put overrides in the selected product file. See the [merge reference](../../../reference/configuration.md#configuration-merge) and [worked example](../../configuration.md#precedence).

Tools and hooks use `runtime.context.get_plugin_config("de-agent")` for a copy of the effective configuration and `runtime.context.session.outputs` for the session output directory. Use `runtime.context.get_model(alias)` and `runtime.context.semantic_client` for host-managed clients.

See the [manifest fields](../../../reference/extension-schema.md#plugin-manifest) and [extension guides](overview.md) to add tools, Skills, subagents, hooks, or human-feedback rules.

## Discovery and refresh

After editing an extension, send a new request to REST or its attached TUI. When preparing the next agent the server checks its fingerprint and attempts refresh if changed. In-flight turns keep their original graphs. Correct any failed refresh before retrying.

With an embedded Python SDK, rerun `prepare_runtime` and `create_agent`. Long-lived custom hosts can use the [dataagent.host refresh API](../../../reference/python-api.md#host-api). See the [extension lifecycle](../../../../development/architecture/extension-lifecycle.md) for compilation, caching, and settings requiring restart.
