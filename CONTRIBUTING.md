# Contributing

Thanks for helping improve this project. See the [developer docs](docs/development/index.md) for setup, architecture, testing, and releases.

## Before You Start

- Open an issue or discussion before changing public APIs, runtime protocol behavior, datasource behavior, security policy, or persisted data formats.
- Keep pull requests focused on one boundary or feature area.
- Update documentation when a change affects setup, configuration, API behavior, datasource support, event streams, artifacts, or user-visible output.

## Local Setup

Follow the [development setup guide](docs/development/setup.md) to install dependencies, run the source checkout, and debug a component.

## Verification

Use the [testing guide](docs/development/testing.md) to select checks for the affected area. For changes to distribution artifacts, follow the [build and release guide](docs/development/release.md).

## Security And Data Hygiene

- Do not commit credentials, local databases, generated storage, private benchmark data, customer data, or personal machine paths.
- Use example values such as `replace-with-your-key` in docs and fixtures.
- Keep browser-facing code free of LLM provider keys and datasource credentials.
- Put public documentation under `docs/`; keep internal planning and private engineering notes outside this repository.

## Pull Request Checklist

- The change is scoped to the issue or stated goal.
- The description explains the scope and any compatibility, migration, or deployment impact.
- The description lists the checks that were run.
- Public docs are updated when behavior changes.
- Relevant smoke checks pass locally.
- No generated build output, local storage, or machine-specific files are included.
