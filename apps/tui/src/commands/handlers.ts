/**
 * Command Handlers Module
 *
 * Implements all slash command handlers for the DataFoundry TUI.
 * Each handler validates arguments, interacts with store/config, and returns formatted results.
 */

import type { ParsedArguments } from './command-parser.js';
import type { TuiAppState } from '../state/store.js';
import type {
  WorkspaceConfigStore,
  WorkspaceConfigKind,
  WorkspaceConfigItem,
} from '../state/data-task-state.js';
import {
  loadWorkspaceConfig,
  persistWorkspaceConfig,
  createWorkspaceConfigItem,
  defaultSettingsForKind,
} from '../state/data-task-state.js';
import { selectLiveSessionView } from '../state/store.js';

/**
 * Command execution result
 */
export interface CommandResult {
  success: boolean;
  message: string;
  data?: unknown;
}

/**
 * Command handler context
 */
export interface HandlerContext {
  /** Parsed command arguments */
  args: ParsedArguments;
  /** Current application state */
  state: TuiAppState;
  /** State store instance */
  store?: {
    getState: () => TuiAppState;
    setWorkspaceConfig: (config: WorkspaceConfigStore) => void;
    updateConfigKind: (
      kind: WorkspaceConfigKind,
      items: WorkspaceConfigItem[]
    ) => void;
  };
}

/**
 * /help command - Display available commands and usage
 */
export function helpCommand(context: HandlerContext): CommandResult {
  const { args } = context;
  const commandName = args.positional[0];

  if (commandName) {
    // Show detailed help for specific command
    const helpText = getCommandHelp(commandName);
    if (!helpText) {
      return {
        success: false,
        message: `Unknown command: ${commandName}`,
      };
    }
    return {
      success: true,
      message: helpText,
    };
  }

  // Show general help
  const helpText = `
Available Commands:

General:
  /help [command]       - Show this help or help for a specific command
  /exit                 - Exit the TUI application
  /clear                - Clear conversation history

Data Management:
  /datasource           - Open data source picker
  /model <action>       - Manage LLM models (list|add|switch)
  /skill <action>       - Manage skills (list|add|switch)
  /mcp <action>         - Manage MCP servers (list|add|switch)
  /kb <action>          - Manage knowledge bases (list|add|upload)

Session:
  /export [file]        - Export conversation to file

Tips:
  - Type a message without '/' to chat with the agent
  - Use Tab for command auto-completion
  - Some commands support subactions like 'list', 'add', 'switch'

For detailed help on a command, use: /help <command>
`.trim();

  return {
    success: true,
    message: helpText,
  };
}

/**
 * Get detailed help for a specific command
 */
function getCommandHelp(commandName: string): string | null {
  const helps: Record<string, string> = {
    datasource: `
/datasource - Open data source picker

Usage:
  /datasource                   - Open the picker

Examples:
  /datasource
`.trim(),

    model: `
/model - Manage LLM models

Usage:
  /model list                   - List all configured models
  /model add                    - Start interactive model configuration
  /model switch <id>            - Switch to a specific model

Examples:
  /model list
  /model switch server-default
`.trim(),

    skill: `
/skill - Manage skills

Usage:
  /skill list                   - List all available skills
  /skill add                    - Start interactive skill upload
  /skill switch <id>            - Switch to a specific skill

Examples:
  /skill list
  /skill switch data-analysis
`.trim(),

    mcp: `
/mcp - Manage MCP servers

Usage:
  /mcp list                     - List all configured MCP servers
  /mcp add                      - Start interactive MCP server setup
  /mcp switch <id>              - Switch to a specific MCP server

Examples:
  /mcp list
  /mcp add
`.trim(),

    kb: `
/kb - Manage knowledge bases

Usage:
  /kb list                      - List all knowledge bases
  /kb add                       - Create a new knowledge base
  /kb upload <kb-id> <file>     - Upload documents to a knowledge base

Examples:
  /kb list
  /kb add
  /kb upload my-kb docs.pdf
`.trim(),

    export: `
/export - Export conversation

Usage:
  /export [filename]            - Export conversation to file
                                  (defaults to conversation-<timestamp>.json)

Examples:
  /export
  /export my-session.json
`.trim(),

    clear: `
/clear - Clear conversation history

Usage:
  /clear                        - Clear all messages from current conversation

Note: This cannot be undone.
`.trim(),

    exit: `
/exit - Exit the application

Usage:
  /exit                         - Exit the TUI application

Aliases: quit, q
`.trim(),
  };

  return helps[commandName] || null;
}

/**
 * /datasource command - Manage data sources
 */
export function datasourceCommand(context: HandlerContext): CommandResult {
  const action = context.args.positional[0];
  if (action && action !== 'help') {
    return {
      success: false,
      message: 'Usage: /datasource',
    };
  }

  return {
    success: true,
    message: 'Loading data sources...',
    data: { action: 'open_datasource_picker' },
  };
}

/**
 * /model command - Manage LLM models
 */
export function modelCommand(context: HandlerContext): CommandResult {
  const { args, state, store } = context;
  const action = args.positional[0] || 'list';

  const workspaceConfig = state.workspaceConfig;

  switch (action) {
    case 'list': {
      const models = workspaceConfig.llm;
      if (models.length === 0) {
        return {
          success: true,
          message: 'No models configured.',
        };
      }

      const lines = ['Available Models:', ''];
      models.forEach((model) => {
        const status = model.enabled ? '✓' : '✗';
        const builtin = model.builtin ? '(builtin)' : '';
        const provider = model.settings?.provider || 'unknown';
        const modelName = model.settings?.modelName || 'unknown';
        lines.push(`  ${status} ${model.id} ${builtin}`);
        lines.push(`    ${model.description}`);
        lines.push(`    Provider: ${provider}, Model: ${modelName}`);
        lines.push('');
      });

      return {
        success: true,
        message: lines.join('\n'),
        data: { models },
      };
    }

    case 'add': {
      return {
        success: true,
        message: `
To add a new model configuration:
1. Open the configuration UI
2. Or manually edit workspace config

Interactive model creation is not yet implemented in CLI mode.
Use the GUI configuration panel for now.
`.trim(),
        data: { action: 'interactive_add_model' },
      };
    }

    case 'switch': {
      const modelId = args.positional[1];
      if (!modelId) {
        return {
          success: false,
          message: 'Usage: /model switch <id>',
        };
      }

      const model = workspaceConfig.llm.find((m) => m.id === modelId);
      if (!model) {
        return {
          success: false,
          message: `Model not found: ${modelId}`,
        };
      }

      // Note: LLM switching logic may need additional state management
      // For now, we'll just confirm the model exists
      return {
        success: true,
        message: `Model configuration found: ${model.name}\n\nNote: Active model switching requires integration with runtime config.`,
        data: { modelId, model },
      };
    }

    default:
      return {
        success: false,
        message: `Unknown action: ${action}. Use: list, add, switch`,
      };
  }
}

/**
 * /skill command - Manage skills
 */
export function skillCommand(context: HandlerContext): CommandResult {
  const { args, state, store } = context;
  const action = args.positional[0] || 'list';

  const workspaceConfig = state.workspaceConfig;

  switch (action) {
    case 'list': {
      const skills = workspaceConfig.skill;
      if (skills.length === 0) {
        return {
          success: true,
          message: 'No skills configured.',
        };
      }

      const lines = ['Available Skills:', ''];
      skills.forEach((skill) => {
        const status = skill.enabled ? '✓' : '✗';
        const builtin = skill.builtin ? '(builtin)' : '';
        const format = skill.settings?.packageFormat || 'unknown';
        lines.push(`  ${status} ${skill.id} ${builtin}`);
        lines.push(`    ${skill.description}`);
        lines.push(`    Format: ${format}`);
        lines.push('');
      });

      return {
        success: true,
        message: lines.join('\n'),
        data: { skills },
      };
    }

    case 'add': {
      return {
        success: true,
        message: `
To add a new skill:
1. Prepare a SKILL.md file with frontmatter
2. Use the GUI to upload the skill package
3. Or use the backend API: POST /api/v1/skills

Interactive skill upload is not yet implemented in CLI mode.
`.trim(),
        data: { action: 'interactive_add_skill' },
      };
    }

    case 'switch': {
      const skillId = args.positional[1];
      if (!skillId) {
        return {
          success: false,
          message: 'Usage: /skill switch <id>',
        };
      }

      const skill = workspaceConfig.skill.find((s) => s.id === skillId);
      if (!skill) {
        return {
          success: false,
          message: `Skill not found: ${skillId}`,
        };
      }

      // Note: Skill switching requires integration with session config
      return {
        success: true,
        message: `Skill found: ${skill.name}\n\nNote: Active skill switching requires integration with runtime config.`,
        data: { skillId, skill },
      };
    }

    default:
      return {
        success: false,
        message: `Unknown action: ${action}. Use: list, add, switch`,
      };
  }
}

/**
 * /mcp command - Manage MCP servers
 */
export function mcpCommand(context: HandlerContext): CommandResult {
  const { args, state, store } = context;
  const action = args.positional[0] || 'list';

  const workspaceConfig = state.workspaceConfig;

  switch (action) {
    case 'list': {
      const mcpServers = workspaceConfig.mcp;
      if (mcpServers.length === 0) {
        return {
          success: true,
          message: 'No MCP servers configured.',
        };
      }

      const lines = ['Available MCP Servers:', ''];
      mcpServers.forEach((mcp) => {
        const status = mcp.enabled ? '✓' : '✗';
        const transport = mcp.settings?.transport || 'unknown';
        const url = mcp.settings?.serverUrl || 'unknown';
        lines.push(`  ${status} ${mcp.id}`);
        lines.push(`    ${mcp.description}`);
        lines.push(`    Transport: ${transport}, URL: ${url}`);
        lines.push('');
      });

      return {
        success: true,
        message: lines.join('\n'),
        data: { mcpServers },
      };
    }

    case 'add': {
      return {
        success: true,
        message: `
To add a new MCP server:
1. Open the configuration UI
2. Configure transport type (SSE, Streamable HTTP, stdio)
3. Provide server URL or command

Interactive MCP server setup is not yet implemented in CLI mode.
Backend MCP support is also pending implementation.
`.trim(),
        data: { action: 'interactive_add_mcp' },
      };
    }

    case 'switch': {
      const mcpId = args.positional[1];
      if (!mcpId) {
        return {
          success: false,
          message: 'Usage: /mcp switch <id>',
        };
      }

      const mcp = workspaceConfig.mcp.find((m) => m.id === mcpId);
      if (!mcp) {
        return {
          success: false,
          message: `MCP server not found: ${mcpId}`,
        };
      }

      return {
        success: true,
        message: `MCP server found: ${mcp.name}\n\nNote: MCP integration is pending backend implementation.`,
        data: { mcpId, mcp },
      };
    }

    default:
      return {
        success: false,
        message: `Unknown action: ${action}. Use: list, add, switch`,
      };
  }
}

/**
 * /kb command - Manage knowledge bases
 */
export function kbCommand(context: HandlerContext): CommandResult {
  const { args, state, store } = context;
  const action = args.positional[0] || 'list';

  const workspaceConfig = state.workspaceConfig;

  switch (action) {
    case 'list': {
      const knowledgeBases = workspaceConfig.kb;
      if (knowledgeBases.length === 0) {
        return {
          success: true,
          message: 'No knowledge bases configured.',
        };
      }

      const lines = ['Available Knowledge Bases:', ''];
      knowledgeBases.forEach((kb) => {
        const status = kb.enabled ? '✓' : '✗';
        const indexName = kb.settings?.indexName || 'unknown';
        const topK = kb.settings?.retrievalTopK || 'default';
        lines.push(`  ${status} ${kb.id}`);
        lines.push(`    ${kb.description}`);
        lines.push(`    Index: ${indexName}, Top K: ${topK}`);
        lines.push('');
      });

      return {
        success: true,
        message: lines.join('\n'),
        data: { knowledgeBases },
      };
    }

    case 'add': {
      return {
        success: true,
        message: `
To add a new knowledge base:
1. Open the configuration UI
2. Provide an index name and retrieval settings

Interactive knowledge base creation is not yet implemented in CLI mode.
Backend knowledge base support is also pending implementation.
`.trim(),
        data: { action: 'interactive_add_kb' },
      };
    }

    case 'upload': {
      const kbId = args.positional[1];
      const filePath = args.positional[2];

      if (!kbId || !filePath) {
        return {
          success: false,
          message: 'Usage: /kb upload <kb-id> <file-path>',
        };
      }

      return {
        success: true,
        message: `
Knowledge base document upload requires backend implementation.
Target KB: ${kbId}
File: ${filePath}

This will be implemented when backend /api/v1/knowledge/upload endpoint is available.
`.trim(),
        data: { kbId, filePath, action: 'upload' },
      };
    }

    default:
      return {
        success: false,
        message: `Unknown action: ${action}. Use: list, add, upload`,
      };
  }
}

/**
 * /export command - Export conversation
 */
export function exportCommand(context: HandlerContext): CommandResult {
  const { args, state } = context;
  const filename = args.positional[0] || `conversation-${Date.now()}.json`;

  const exportData = {
    threadId: state.threadId,
    messages: state.messages,
    artifacts: state.artifacts,
    events: state.events,
    stats: selectLiveSessionView(state),
    exportedAt: new Date().toISOString(),
  };

  return {
    success: true,
    message: `
Conversation export prepared.
Filename: ${filename}

To complete the export, the data needs to be written to disk.
This functionality requires file system access integration.

Export data includes:
  - ${state.messages.length} messages
  - ${state.artifacts.length} artifacts
  - ${state.events.length} events
  - Session statistics
`.trim(),
    data: { filename, exportData },
  };
}

/**
 * /clear command - Clear conversation history
 */
export function clearCommand(context: HandlerContext): CommandResult {
  return {
    success: true,
    message: 'Conversation history will be cleared.',
    data: { action: 'clear_history' },
  };
}

/**
 * /exit command - Exit the application
 */
export function exitCommand(context: HandlerContext): CommandResult {
  return {
    success: true,
    message: 'Exiting DataFoundry TUI. Goodbye!',
    data: { action: 'exit_application' },
  };
}

/**
 * Command handler registry
 */
export const commandHandlers = {
  help: helpCommand,
  datasource: datasourceCommand,
  model: modelCommand,
  skill: skillCommand,
  mcp: mcpCommand,
  kb: kbCommand,
  export: exportCommand,
  clear: clearCommand,
  exit: exitCommand,
} as const;

/**
 * Execute a command by name
 */
export function executeCommand(
  commandName: string,
  context: HandlerContext
): CommandResult {
  const handler = commandHandlers[commandName as keyof typeof commandHandlers];

  if (!handler) {
    return {
      success: false,
      message: `Unknown command: ${commandName}. Type /help for available commands.`,
    };
  }

  try {
    return handler(context);
  } catch (error) {
    return {
      success: false,
      message: `Error executing command: ${error instanceof Error ? error.message : String(error)}`,
    };
  }
}
