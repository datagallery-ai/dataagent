# Contributing

Thanks for helping improve this project.

## Before You Start

- Open an issue or discussion before changing public APIs, runtime protocol behavior, datasource behavior, security policy, or persisted data formats.
- Keep pull requests focused on one boundary or feature area.
- Update documentation when a change affects setup, configuration, API behavior, datasource support, event streams, artifacts, or user-visible output.

## Local Setup

```bash
npm install
cp .env.example .env
cp apps/web/.env.example apps/web/.env.local
npm run dev
```

Open the workbench at <http://127.0.0.1:3000/data-tasks>.

## Verification

Run the broad checks before opening a larger pull request:

```bash
npm run build
npm run smoke:config-api
npm run smoke:data-gateway
npm run smoke:copilotkit
npm run smoke:docs
```

For smaller changes, run the targeted script that matches the package you touched. The root `package.json` lists the available checks.

## Security And Data Hygiene

- Do not commit credentials, local databases, generated storage, private benchmark data, customer data, or personal machine paths.
- Use example values such as `replace-with-your-key` in docs and fixtures.
- Keep browser-facing code free of LLM provider keys and datasource credentials.
- Put public documentation under `docs/`; keep internal planning and private engineering notes outside this repository.

## Pull Request Checklist

- The change is scoped to the issue or stated goal.
- Public docs are updated when behavior changes.
- Relevant smoke checks pass locally.
- No generated build output, local storage, or machine-specific files are included.
