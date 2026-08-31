/**
 * Keyboard shortcuts system for DataFoundry TUI
 * Provides centralized keybinding management with history, completion, and navigation
 */

export interface KeybindingAction {
  key: string;
  description: string;
  category: 'navigation' | 'session' | 'system' | 'input';
}

/**
 * Command history manager
 * Tracks user input history with navigation support
 */
export class CommandHistory {
  private history: string[] = [];
  private currentIndex: number = -1;
  private draft: string = '';
  private maxSize: number;

  constructor(maxSize: number = 100) {
    this.maxSize = maxSize;
  }

  /**
   * Add a command to history
   */
  add(command: string): void {
    if (!command.trim()) return;

    // Remove duplicate if it's the last command
    if (this.history[this.history.length - 1] === command) return;

    this.history.push(command);

    // Maintain max size
    if (this.history.length > this.maxSize) {
      this.history.shift();
    }

    // Reset navigation index
    this.currentIndex = -1;
  }

  /**
   * Navigate to previous command (↑)
   */
  previous(draft: string = ''): string | null {
    if (this.history.length === 0) return null;

    if (this.currentIndex === -1) {
      this.draft = draft;
      this.currentIndex = this.history.length - 1;
    } else if (this.currentIndex > 0) {
      this.currentIndex--;
    }

    return this.history[this.currentIndex];
  }

  /**
   * Navigate to next command (↓)
   */
  next(): string | null {
    if (this.history.length === 0 || this.currentIndex === -1) return null;

    if (this.currentIndex < this.history.length - 1) {
      this.currentIndex++;
      return this.history[this.currentIndex];
    } else {
      // At the end, restore the text that was present before navigation.
      const draft = this.draft;
      this.currentIndex = -1;
      this.draft = '';
      return draft;
    }
  }

  /**
   * Reset navigation index
   */
  reset(): void {
    this.currentIndex = -1;
    this.draft = '';
  }

  /**
   * Get all history
   */
  getAll(): string[] {
    return [...this.history];
  }

  /**
   * Clear all history
   */
  clear(): void {
    this.history = [];
    this.currentIndex = -1;
    this.draft = '';
  }

  /**
   * Get current position in history (-1 means not navigating)
   */
  getCurrentIndex(): number {
    return this.currentIndex;
  }
}

/**
 * Command completion manager
 * Provides tab completion for common commands
 */
export class CommandCompletion {
  private commands: string[] = [];
  private currentCompletions: string[] = [];
  private completionIndex: number = -1;
  private lastInput: string = '';

  constructor(commands: string[] = []) {
    this.commands = commands;
  }

  /**
   * Update available commands
   */
  setCommands(commands: string[]): void {
    this.commands = commands;
  }

  /**
   * Get completion suggestions for input
   */
  complete(input: string): string | null {
    // If input changed, reset completion state
    if (input !== this.lastInput) {
      this.lastInput = input;
      this.completionIndex = -1;
      this.currentCompletions = this.findMatches(input);
    }

    if (this.currentCompletions.length === 0) return null;

    // Cycle through completions
    this.completionIndex = (this.completionIndex + 1) % this.currentCompletions.length;
    return this.currentCompletions[this.completionIndex];
  }

  /**
   * Find matching commands
   */
  private findMatches(input: string): string[] {
    if (!input.trim()) return [];

    const lowercaseInput = input.toLowerCase();
    return this.commands.filter(cmd =>
      cmd.toLowerCase().startsWith(lowercaseInput)
    );
  }

  /**
   * Reset completion state
   */
  reset(): void {
    this.currentCompletions = [];
    this.completionIndex = -1;
    this.lastInput = '';
  }

  /**
   * Get all available completions for current input
   */
  getCompletions(input: string): string[] {
    return this.findMatches(input);
  }
}

/**
 * Keybinding definitions
 */
export const KEYBINDINGS: KeybindingAction[] = [
  // System shortcuts
  { key: 'Ctrl+C', description: 'Clear input, press again to exit', category: 'system' },
  { key: 'Ctrl+L', description: 'Clear screen', category: 'system' },
  { key: 'Ctrl+O', description: 'Toggle compact tool/thinking view', category: 'system' },
  { key: 'Alt+T', description: 'Toggle thinking expansion', category: 'system' },

  // Session shortcuts
  { key: 'Ctrl+N', description: 'New session', category: 'session' },
  { key: 'Ctrl+R', description: 'Reset session', category: 'session' },

  // Input shortcuts
  { key: 'Terminal paste', description: 'Paste text into input', category: 'input' },
  { key: '↑', description: 'Previous command', category: 'input' },
  { key: '↓', description: 'Next command', category: 'input' },
  { key: 'Tab (input)', description: 'Command completion', category: 'input' },
  { key: 'Enter', description: 'Send message', category: 'input' },
  { key: 'Backspace', description: 'Delete character', category: 'input' },
];

/**
 * Get keybindings by category
 */
export function getKeybindingsByCategory(category: KeybindingAction['category']): KeybindingAction[] {
  return KEYBINDINGS.filter(kb => kb.category === category);
}

/**
 * Format keybinding for display
 */
export function formatKeybinding(keybinding: KeybindingAction): string {
  return `${keybinding.key}: ${keybinding.description}`;
}

/**
 * Get all keybindings formatted as help text
 */
export function getKeybindingsHelp(): string {
  const categories: Array<{ title: string; category: KeybindingAction['category'] }> = [
    { title: 'System', category: 'system' },
    { title: 'Navigation', category: 'navigation' },
    { title: 'Session', category: 'session' },
    { title: 'Input', category: 'input' },
  ];

  return categories
    .map(({ title, category }) => ({
      title,
      bindings: getKeybindingsByCategory(category),
    }))
    .filter(({ bindings }) => bindings.length > 0)
    .map(({ title, bindings }) => {
      const lines = bindings.map(kb => `  ${formatKeybinding(kb)}`);
      return `${title}:\n${lines.join('\n')}`;
    })
    .join('\n\n');
}

/**
 * Get compact shortcuts for status bar
 */
export function getStatusBarShortcuts(): Array<{ key: string; action: string }> {
  return [
    { key: '↑/↓', action: 'History' },
    { key: 'Alt+T', action: 'Thinking' },
    { key: 'Ctrl+O', action: 'Compact' },
    { key: 'Ctrl+N', action: 'New' },
    { key: 'Ctrl+L', action: 'Clear' },
    { key: 'Ctrl+C', action: 'Clear / Exit' },
  ];
}

/**
 * Default command suggestions
 */
export const DEFAULT_COMMANDS = [
  '/outputs',
  '/output',
  '/datasource',
  '/skill',
  '/skill list',
  '/skill show',
  '/skill select ',
  '/help',
  '/status',
  '/resume',
  '/clear',
  '/reset',
  '/exit',
  'show tables',
  'describe table',
  'show schema',
  'explain query',
  'show stats',
  'show history',
];
