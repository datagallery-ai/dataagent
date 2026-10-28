/**
 * Command system types
 */

import type { DisplayMessage } from '../state/tui-state.js';
import type { WorkspaceConfigStore } from '../state/data-task-state.js';
import type { ConfigClient } from '../config/index.js';

export interface CommandResult {
  success: boolean;
  message: string;
  data?: unknown;
}

export interface Command {
  name: string;
  description: string;
  aliases?: string[];
  execute: (args: string[], context: CommandContext) => Promise<CommandResult>;
}

export interface CommandContext {
  client: unknown;
  configClient?: ConfigClient | undefined;
  datasourceId?: string | undefined;
  activeSkillId?: string | undefined;
  workspaceConfig: WorkspaceConfigStore;
  state: {
    threadId?: string | undefined;
    messages: DisplayMessage[];
  };
}
