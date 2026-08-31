# Slash Command Popover 使用指南

## 快速开始

### 1. 触发命令面板

在输入框中输入 `/`：

```
┃ /█
```

命令面板会自动在输入框上方展开：

```
╭─────────────────────────────────────────────────╮
│ Slash Commands (↑↓ to navigate, Enter to select, Esc to close) │
│                                                  │
│ ▶ /help (h, ?)                                   │
│   Show available commands                        │
│                                                  │
│   /clear (c)                                     │
│   Clear chat history                             │
│                                                  │
│   /status (s)                                    │
│   Show current session status                    │
│                                                  │
│   /outputs (output)                              │
│   Show outputs for the current session           │
│                                                  │
│   /datasource (ds)                               │
│   Open datasource picker                         │
│                                                  │
│   /skill (skills)                                │
│   List or select available skills                │
│                                                  │
│   /reset (r)                                     │
│   Reset session and start fresh                  │
│                                                  │
│   /resume                                        │
│   Resume a server session                        │
│                                                  │
│   ... and 1 more                                 │
╰─────────────────────────────────────────────────╯
┃ /█
```

### 2. 导航命令列表

使用 **↑** 和 **↓** 箭头键在命令之间移动：

```
╭─────────────────────────────────────────────────╮
│ Slash Commands (↑↓ to navigate, Enter to select, Esc to close) │
│                                                  │
│   /help (h, ?)                                   │
│   Show available commands                        │
│                                                  │
│ ▶ /clear (c)                                     │  ← 当前选中
│   Clear chat history                             │
│                                                  │
│   /status (s)                                    │
│   Show current session status                    │
╰─────────────────────────────────────────────────╯
```

### 3. 选择命令

按 **Enter** 键选择当前高亮的命令，它会自动填充到输入框：

```
┃ /clear █
```

### 4. 过滤命令

继续输入可以过滤命令列表。例如，输入 `/he`：

```
╭─────────────────────────────────────────────────╮
│ Slash Commands (↑↓ to navigate, Enter to select, Esc to close) │
│                                                  │
│ ▶ /help (h, ?)                                   │
│   Show available commands                        │
╰─────────────────────────────────────────────────╯
┃ /he█
```

只显示匹配的命令（命令名或别名以 "he" 开头）。

### 5. 关闭面板

按 **Esc** 键关闭命令面板，保持当前输入：

```
┃ /he█
```

## 键盘快捷键

当命令面板**打开**时：
- **↑** - 上一个命令
- **↓** - 下一个命令  
- **Enter** - 选择当前命令
- **Esc** - 关闭面板
- **继续输入** - 过滤命令

当命令面板**关闭**时：
- **↑** - 历史记录中的上一条命令
- **↓** - 历史记录中的下一条命令
- **Tab** - 自动补全
- **Enter** - 提交输入
- **Ctrl+C** - 清空输入 / 退出

## 使用技巧

### 快速过滤

如果你知道命令名称，直接输入前几个字母快速定位：

- `/h` → help
- `/s` → status, skill
- `/cl` → clear
- `/d` → datasource

### 别名支持

命令面板也会根据别名进行过滤。例如：

- `/h` 会匹配 `/help` （别名 h）
- `/s` 会匹配 `/status` （别名 s）
- `/ds` 会匹配 `/datasource` （别名 ds）

### 查看所有命令

输入单个 `/` 可以看到所有可用命令的完整列表。

### 了解命令详情

每个命令都会显示：
1. **命令名称**：如 `/help`
2. **别名**（如果有）：如 `(h, ?)`
3. **描述**：简短说明命令的功能

## 常用命令速查

| 命令 | 别名 | 功能 |
|------|------|------|
| `/help` | h, ? | 显示帮助信息 |
| `/clear` | c | 清空对话历史 |
| `/status` | s | 显示会话状态 |
| `/datasource` | ds | 打开数据源选择器 |
| `/skill` | skills | 列出或选择技能 |
| `/reset` | r | 重置会话 |
| `/resume` | - | 恢复服务器会话 |
| `/outputs` | output | 显示输出 |
| `/exit` | - | 退出应用 |

## 故障排除

### 面板没有显示
- 确保输入以 `/` 开头
- 检查输入框是否处于激活状态（不是 disabled 状态）

### 导航键不工作
- 确保面板已经打开（看到命令列表）
- 检查终端是否支持原始输入模式

### 过滤不工作
- 过滤是基于前缀匹配的，不是模糊匹配
- 确保输入的字符与命令名或别名的开头匹配

## 反馈和改进

如有问题或建议，请参考 `SLASH_COMMAND_POPOVER.md` 中的改进计划。
