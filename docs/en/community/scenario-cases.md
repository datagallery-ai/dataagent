<a id="scenario-cases"></a>

# Submit a scenario

Start with the [SQLite tutorial](../guides/sqlite-tutorial.md) to query sample orders and verify the resulting CSV.

Scenario cases record a way of using the product. The current control plane has no built-in datasource registry or read-only SQL gateway. If a case depends on those, name the plugin. Do not describe them as control-plane routes.

## What makes a good case

A strong case should be concrete enough to reproduce and polished enough to show externally:

- A recognizable business or operational scenario.
- A small demo-safe dataset or schema outline.
- A clear analysis path, including the questions a user would ask.
- Expected findings that can be validated from the data.
- Notes about governance, privacy, access control, or production boundaries.

## How to propose one

Open a [GitHub issue](https://github.com/datagallery-lab/datafoundry/issues) with:

- Case title and target audience.
- Business question or workflow goal.
- Tables, files, APIs, or external systems involved.
- Expected analysis steps and representative outputs.
- What must be anonymized, mocked, or excluded.

Cases should not require private data access. If the real scenario is sensitive, describe the workflow shape and provide a sanitized dataset or synthetic equivalent.
