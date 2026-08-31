import { themeManager } from './themes/theme-manager.js';
import type { TuiThemeTokens } from './themes/types.js';

/**
 * 动态语义主题访问层。组件应优先使用这里的语义分组，而不是直接写色值。
 */
export const tuiTheme: TuiThemeTokens = {
  get background() {
    return themeManager.getTokens().background;
  },
  get text() {
    return themeManager.getTokens().text;
  },
  get border() {
    return themeManager.getTokens().border;
  },
  get structure() {
    return themeManager.getTokens().structure;
  },
  get interaction() {
    return themeManager.getTokens().interaction;
  },
  get status() {
    return themeManager.getTokens().status;
  },
  get selection() {
    return themeManager.getTokens().selection;
  },
};

/**
 * 颜色使用映射
 * 定义了各个UI元素应该使用的颜色
 */
export const colorUsage = {
  get appTitle() { return tuiTheme.structure.accent; },
  get selectedItem() { return tuiTheme.selection.selectedTitle; },
  get activeMode() { return tuiTheme.interaction.accent; },
  get activeBorder() { return tuiTheme.border.focused; },
  get fileName() { return tuiTheme.structure.accent; },
  get resourceName() { return tuiTheme.text.primary; },
  get connected() { return tuiTheme.status.success; },
  get running() { return tuiTheme.status.warning; },
  get completed() { return tuiTheme.status.success; },
  get failed() { return tuiTheme.status.error; },
  get idle() { return tuiTheme.text.secondary; },
  get toolSuccess() { return tuiTheme.status.success; },
  get toolRunning() { return tuiTheme.status.warning; },
  get toolFailed() { return tuiTheme.status.error; },
  get toolName() { return tuiTheme.text.secondary; },
  get systemNotice() { return tuiTheme.text.secondary; },
  get outputsNotice() { return tuiTheme.text.secondary; },
  get timestamp() { return tuiTheme.text.secondary; },
  get metadata() { return tuiTheme.text.secondary; },
  get dimText() { return tuiTheme.text.secondary; },
};

/**
 * Ink 颜色映射。
 *
 * Ink/Chalk 支持 hex 字符串，这里直接暴露低饱和主题色，避免组件
 * 退回到高饱和的 cyan/yellow/green。
 */
export const inkColors = {
  get background() { return tuiTheme.background.canvas; },
  get surface() { return tuiTheme.background.surface; },
  get border() { return tuiTheme.border.default; },
  get focus() { return tuiTheme.border.focused; },
  get accent() { return tuiTheme.structure.accent; },
  get success() { return tuiTheme.status.success; },
  get warning() { return tuiTheme.status.warning; },
  get error() { return tuiTheme.status.error; },
  get emphasis() { return tuiTheme.text.emphasis; },
  get muted() { return tuiTheme.text.secondary; },
  get subtle() { return tuiTheme.text.muted; },
  get text() { return tuiTheme.text.primary; },
};

/**
 * 所有命令补全和资源选择界面共享这一语义层。
 */
export const selectionColors = {
  get background() { return tuiTheme.selection.background; },
  get selectedBackground() { return tuiTheme.selection.selectedBackground; },
  get border() { return tuiTheme.selection.border; },
  get heading() { return tuiTheme.selection.heading; },
  get selectedTitle() { return tuiTheme.selection.selectedTitle; },
  get title() { return tuiTheme.selection.title; },
  get accent() { return tuiTheme.selection.accent; },
  get selectedDescription() { return tuiTheme.selection.selectedDescription; },
  get description() { return tuiTheme.selection.description; },
  get disabled() { return tuiTheme.selection.disabled; },
};

/**
 * 获取状态对应的颜色
 */
export function getStatusColor(status: string): string {
  switch (status) {
    case 'success':
    case 'completed':
    case 'connected':
      return inkColors.success;
    case 'running':
    case 'pending':
      return inkColors.warning;
    case 'failed':
    case 'error':
    case 'disconnected':
      return inkColors.error;
    case 'cancelled':
    case 'idle':
    default:
      return inkColors.muted;
  }
}

export function formatBytes(bytes: number): string {
  const safeBytes = Math.max(0, bytes);
  if (safeBytes < 1024) return `${safeBytes} B`;
  const kib = safeBytes / 1024;
  if (kib < 1024) return `${kib.toFixed(kib < 10 ? 1 : 0)} KB`;
  const mib = kib / 1024;
  return `${mib.toFixed(mib < 10 ? 1 : 0)} MB`;
}

export function basename(path: string): string {
  const normalized = path.replace(/\\/gu, '/').replace(/\/+$/u, '');
  return normalized.split('/').pop() || normalized;
}

/**
 * 颜色使用指南
 *
 * ✅ 推荐使用：
 * - selectionColors: 命令补全、资源选择、会话选择和产出选择
 * - inkColors.accent: 主界面结构、链接、产出等蓝色结构语义
 * - tuiTheme.interaction.accent: 雾青色操作语义
 * - success: 工具执行成功、连接正常
 * - warning: 真正需要用户注意的警告（不是所有运行中状态）
 * - text.secondary / text.muted: 时间戳、快捷键、系统通知、次要信息
 *
 * ❌ 避免使用：
 * - magenta/purple: 已移除，用 muted 或 accent 替代
 * - 同时使用多种高亮色在相邻元素上
 * - 大面积背景色（除非是真正的错误/警告）
 *
 * 示例：
 * ```tsx
 * import { inkColors, selectionColors, getStatusColor } from './theme.js';
 *
 * // 选中项
 * <Box backgroundColor={selected ? selectionColors.selectedBackground : selectionColors.background}>
 *   <Text color={selected ? selectionColors.selectedTitle : selectionColors.title}>Item</Text>
 * </Box>
 *
 * // 状态指示
 * <Text color={getStatusColor(toolCall.status)}>✓</Text>
 *
 * // 次要信息
 * <Text dimColor>2.3s</Text>
 * ```
 */
