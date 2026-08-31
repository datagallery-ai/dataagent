# Slash Command Popover Enhancement

## 概述

本次改进为 DataFoundry TUI 添加了类似 OpenCode 的 slash 命令补全弹窗功能。当用户输入 `/` 时，会在输入框上方展开一个详细的命令面板，显示所有可用命令及其描述。

## 改进前后对比

### 改进前
- 所有命令提示压缩在输入框下方一行显示
- 只能显示命令名称，没有描述信息
- 信息密度低，不易浏览

### 改进后
- 在输入框上方展开完整的命令面板
- 显示命令名称、别名和详细描述
- 支持键盘导航和实时过滤
- 提供更好的视觉反馈

## 功能特性

### 1. 自动触发
- 当用户输入 `/` 时自动显示命令面板
- 实时过滤：继续输入可过滤命令列表（如 `/he` 只显示 help 相关命令）

### 2. 键盘导航
- **↑/↓ 箭头键**：上下导航命令列表
- **Enter**：选择当前高亮的命令
- **Esc**：关闭命令面板
- **继续输入**：过滤命令列表

### 3. 视觉设计
- 使用 `inkColors.accent` 配色方案，与应用主题统一
- 当前选中项带有 `▶` 标记和加粗样式
- 显示命令别名（如果有）
- 限制最多显示 8 个命令，避免界面溢出

### 4. 智能过滤
- 支持按命令名称前缀过滤
- 支持按别名前缀过滤
- 过滤不区分大小写

## 技术实现

### 新增文件

#### `src/ui/SlashCommandPopover.tsx`
命令弹窗组件，负责：
- 渲染命令列表
- 显示当前选中状态
- 处理命令过滤逻辑

### 修改文件

#### `src/ui/InputBox.tsx`
集成命令弹窗，新增功能：
- 监听 `/` 输入，自动显示/隐藏弹窗
- 处理弹窗打开时的特殊键盘导航
- 管理弹窗状态（显示/隐藏、当前选中索引）
- 命令选择后自动填充到输入框

### 关键状态管理

```typescript
// 弹窗显示状态
const [showSlashPopover, setShowSlashPopover] = useState(false);

// 当前选中的命令索引
const [slashPopoverActiveIndex, setSlashPopoverActiveIndex] = useState(0);

// 从 CommandProcessor 获取所有可用命令
const availableCommands: Command[] = React.useMemo(() => {
  return commandProcessor.getCommands();
}, []);
```

### 交互逻辑

1. **显示触发**：
   ```typescript
   useEffect(() => {
     const trimmed = localValue.trim();
     if (trimmed.startsWith('/') && !disabled) {
       setShowSlashPopover(true);
       setSlashPopoverActiveIndex(0);
     } else {
       setShowSlashPopover(false);
     }
   }, [localValue, disabled]);
   ```

2. **键盘导航**：
   - 弹窗打开时，↑↓ 键用于在命令列表中导航
   - 弹窗关闭时，↑↓ 键用于历史命令导航
   - Enter 键在弹窗打开时选择命令，关闭时提交输入

3. **命令选择**：
   ```typescript
   const selectSlashCommand = (index: number) => {
     const filtered = getFilteredCommands();
     const cmd = filtered[index];
     if (cmd) {
       setLocalValue(`/${cmd.name} `);
       setShowSlashPopover(false);
     }
   };
   ```

## 使用示例

### 基本使用
1. 在输入框中输入 `/`
2. 命令面板自动弹出
3. 使用 ↑↓ 键选择命令
4. 按 Enter 确认选择

### 过滤命令
1. 输入 `/help` - 只显示 help 命令
2. 输入 `/s` - 显示所有以 's' 开头的命令（status, skill, etc.）

### 快速访问
1. 输入 `/h` - 快速过滤到 help
2. 按 Enter - 自动填充 `/help `

## 可用命令列表

命令面板会显示所有已注册的命令，包括：

- `/help` (h, ?) - Show available commands
- `/clear` (c) - Clear chat history
- `/status` (s) - Show current session status
- `/outputs` (output) - Show outputs for the current session
- `/datasource` (ds) - Open datasource picker
- `/skill` (skills) - List or select available skills
- `/reset` (r) - Reset session and start fresh
- `/resume` - Resume a server session
- `/exit` - Exit the TUI application

## 参考实现

本实现参考了 OpenCode v1.17.14 的 `slash-popover.tsx` 组件设计：
- 文件位置：`/data2/zhangh/code/dev_datafoundry/ref/opencode-v1.17.14/packages/app/src/components/prompt-input/slash-popover.tsx`
- 主要借鉴：
  - 弹窗布局和样式设计
  - 命令列表展示方式
  - 键盘导航交互模式

## 适配说明

由于 DataFoundry TUI 使用 Ink（终端 UI 库），而 OpenCode 使用 SolidJS（Web UI），实现上做了以下适配：

1. **布局适配**：
   - Web: 使用 absolute positioning 和 transform
   - Terminal: 使用 flexbox 和组件顺序

2. **交互适配**：
   - Web: 支持鼠标悬停 (onPointerMove)
   - Terminal: 仅键盘导航

3. **样式适配**：
   - Web: CSS 类和 Tailwind
   - Terminal: Ink Box 组件和 borderStyle

## 测试

运行测试脚本：
```bash
cd /data2/zhangh/code/dev_datafoundry/datafoundry/apps/tui
tsx test-slash-popover.tsx
```

测试要点：
- [ ] 输入 `/` 后弹窗正确显示
- [ ] ↑↓ 键可以导航命令列表
- [ ] Enter 键可以选择命令
- [ ] Esc 键可以关闭弹窗
- [ ] 输入 `/help` 等可以正确过滤
- [ ] 命令选择后正确填充到输入框

## 未来改进

1. **性能优化**：
   - 命令列表缓存
   - 过滤结果 memoization

2. **功能增强**：
   - 显示命令快捷键（如果有）
   - 支持模糊匹配（不仅是前缀匹配）
   - 命令分组显示

3. **视觉改进**：
   - 为不同类型的命令添加图标
   - 高亮匹配的文本部分

## 相关文件

- `src/ui/SlashCommandPopover.tsx` - 命令弹窗组件
- `src/ui/InputBox.tsx` - 输入框组件（已修改）
- `src/commands/CommandProcessor.ts` - 命令处理器
- `src/commands/builtinCommands.ts` - 内置命令定义
- `src/ui/theme.ts` - 主题配色
- `test-slash-popover.tsx` - 测试脚本

## 截图位置

参考截图：
- 当前实现：`/home/zhangh/code/dev_datafoundry/slash命令的补全提示.png`
- 期望效果：`/home/zhangh/code/dev_datafoundry/slash命令补全参考_opencode.png`
