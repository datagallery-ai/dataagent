# Skill

Skill 是一个目录，里面有 `SKILL.md`。模型先看到名称和描述，需要时再读取正文。

## 最小文件

目录名和 frontmatter 的 `name` 保持一致：

```markdown
---
name: number-summary
description: Compute and explain descriptive statistics for an explicit list of numbers.
---

# Number summary

1. Extract the numeric list.
2. Call `common__summarize_numbers`.
```

示例：[number-summary](../../examples/plugins/common/skills/number-summary/SKILL.md)。

`name` 和 `description` 必填。缺一则编译失败。文件超过 10 MiB，或 frontmatter 不是合法 YAML 映射，同样失败。

名称建议：小写字母、数字和单个连字符，最长 64，且等于目录名。不符合时解析器只记警告，Skill 仍会装入。描述超过 1024 个字符时会被截断。可选字段有 `license`、`compatibility`、`metadata` 和 `allowed-tools`。

正文使用 Deep Agents 的 `SKILL.md` 格式。这里不把 frontmatter 改成 JSON。

## 放在哪里

| 位置 | 发现方式 |
| --- | --- |
| `<home>/skills/<name>/SKILL.md` | 自动发现。来源标签是 `User` |
| 已启用插件清单的 `skills` 数组 | 每项是插件根下的目录。来源标签是插件 ID |
| `--config` 旁边的 `skills/` | 不会自动发现 |

Home Skill 和插件 Skill 一起编译。两个来源指向同一真实文件时可以接受。同名却指向不同文件时，编译失败。

插件清单示例：

```json
{
  "skills": ["skills"]
}
```

这表示扫描插件下的 `skills/`，其中每个子目录一份 `SKILL.md`。

## 作用域

根 Agent 使用编译后的全部 Skill 来源。

子代理：

- 省略 `skills`：继承根 Agent 已编译的来源
- 写出路径列表：只使用这些来源，路径相对插件根
- `"skills": []`：不加载 Skill

画像若仍注入默认 `general-purpose`，它继承父级 Skill。插件自己声明了同名 `general-purpose` 时，默认那一个不会再加入。

扩展刷新后，Skill 元数据按新的修订号重新扫描。正文不写入 Checkpoint。
