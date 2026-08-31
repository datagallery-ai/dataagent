# Contributing

DataFoundry welcomes focused contributions that improve the workbench, docs, examples, connectors, and runtime reliability.

## What to contribute

- Product and documentation fixes that make the first-run experience clearer.
- Reproducible bugs with logs, traces, and screenshots.
- Datasource or connector improvements with clear setup and validation notes.
- Scenario cases that show how teams use DataFoundry for real data-agent workflows.
- Tests and smoke checks that protect core behavior such as read-only SQL execution, datasource registration, and traceability.

## Pull request expectations

Keep pull requests narrow and easy to review:

1. Link the issue or describe the user-facing problem.
2. Explain the implementation scope.
3. Include tests, smoke checks, or documentation validation.
4. Call out compatibility, migration, or deployment impact.
5. Avoid bundling unrelated formatting or refactors.

Read the repository-level [CONTRIBUTING.md](https://github.com/datagallery-lab/datafoundry/blob/main/CONTRIBUTING.md) before opening a pull request.

## Local validation

For docs-only changes, run:

```bash
npm run docs:build
npm run smoke:docs
```

For product or runtime changes, add the relevant build, typecheck, test, or smoke command in the pull request description.
