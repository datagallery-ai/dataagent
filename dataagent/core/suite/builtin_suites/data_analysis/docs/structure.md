# Native Suite structure

- `suite.yaml` is the discovery and activation metadata.
- `tools/tools.yaml` registers the four workflow tools.
- `subagents/*.yaml` contains complete Flex configurations with inline role prompts.
- `skills/` contains four loader roots. Each root retains a lightweight facade
  `SKILL.md` for Suite discovery and has one nested actual Skill directory, so a
  child config selects exactly one complete Skill through the existing string
  form of `TOOLS.skills.custom_dirs`.
- `governance/governance.yaml` guards main-Agent orchestration; the subagent YAMLs
  apply the resource-job Python policy directly.
- `resources/`, `data_models/`, `fixtures/`, and `docs/` are packaged Suite assets.
