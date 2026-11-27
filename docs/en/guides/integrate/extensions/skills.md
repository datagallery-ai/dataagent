# Skills

A Skill is a directory containing `SKILL.md`. The model initially sees the name and description and can read the full instructions on demand.

## Minimal file

Use the same name for the directory and the frontmatter's `name`:

```markdown
---
name: number-summary
description: Compute and explain descriptive statistics for an explicit list of numbers.
---

# Number summary

1. Extract the numeric list.
2. Call `common__summarize_numbers`.
```

See the [number-summary example](../../../../../examples/plugins/common/skills/number-summary/SKILL.md) and [Skill metadata](../../../reference/extension-schema.md#skill-metadata) for the Deep Agents format.

## Discovery

| Location | Discovery |
| --- | --- |
| `<home>/skills/<name>/SKILL.md` | Automatic. Source label is `User` |
| `skills` in an enabled plugin manifest | Each entry is a directory under the plugin root. Source label is the plugin ID |
| `skills/` next to an explicit `--config` | No automatic discovery |

Home and plugin Skills compile together. Two sources may point to the same real file. Identical names pointing to different files fail compilation.

Manifest example:

```yaml
skills:
- skills
```

This scans `skills/` below the plugin root, with one `SKILL.md` per child directory.

## Scope

The root agent uses all compiled sources. A subagent that omits `skills` inherits the root sources. A list uses only those plugin-relative paths. `skills: []` loads none. The built-in `general-purpose` inherits its parent's sources. A plugin declaration with that name uses the plugin's settings.

After an extension refresh, Skill metadata is scanned for the new revision. The body is not stored in checkpoints.
