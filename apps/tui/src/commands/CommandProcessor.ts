/**
 * Command processor for handling slash commands
 */

import type { Command, CommandResult, CommandContext } from './types.js';
import { builtinCommands } from './builtinCommands.js';

export class CommandProcessor {
  private commands: Map<string, Command> = new Map();

  constructor() {
    // Register built-in commands
    this.registerCommands(builtinCommands);
  }

  /**
   * Register a command
   */
  registerCommand(command: Command): void {
    this.commands.set(command.name, command);

    // Register aliases
    if (command.aliases) {
      for (const alias of command.aliases) {
        this.commands.set(alias, command);
      }
    }
  }

  /**
   * Register multiple commands
   */
  registerCommands(commands: Command[]): void {
    for (const command of commands) {
      this.registerCommand(command);
    }
  }

  /**
   * Check if input is a command (starts with /)
   */
  isCommand(input: string): boolean {
    return input.trim().startsWith('/');
  }

  /**
   * Parse command input into command name and arguments
   */
  parseCommand(input: string): { commandName: string; args: string[] } {
    const trimmed = input.trim();

    // Remove leading /
    const withoutSlash = trimmed.startsWith('/') ? trimmed.slice(1) : trimmed;

    // Split into parts (respecting quotes)
    const parts = this.splitCommandLine(withoutSlash);

    const commandName = parts[0] || '';
    const args = parts.slice(1);

    return { commandName, args };
  }

  /**
   * Check whether a command or alias is already registered.
   */
  hasCommand(commandName: string): boolean {
    return this.commands.has(commandName);
  }

  /**
   * Execute a command
   */
  async executeCommand(input: string, context: CommandContext): Promise<CommandResult> {
    try {
      const { commandName, args } = this.parseCommand(input);

      if (!commandName) {
        return {
          success: false,
          message: 'No command specified. Type /help for available commands.',
        };
      }

      const command = this.commands.get(commandName);

      if (!command) {
        return {
          success: false,
          message: `Unknown command: ${commandName}. Type /help for available commands.`,
        };
      }

      return await command.execute(args, context);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      return {
        success: false,
        message: `Command execution failed: ${errorMessage}`,
      };
    }
  }

  /**
   * Get all registered commands
   */
  getCommands(): Command[] {
    const uniqueCommands = new Map<string, Command>();

    for (const [key, command] of this.commands) {
      if (key === command.name) {
        uniqueCommands.set(command.name, command);
      }
    }

    return Array.from(uniqueCommands.values());
  }

  /**
   * Split command line into parts, respecting quotes
   */
  private splitCommandLine(input: string): string[] {
    const parts: string[] = [];
    let current = '';
    let inQuotes = false;
    let quoteChar = '';

    for (let i = 0; i < input.length; i++) {
      const char = input[i];

      if ((char === '"' || char === "'") && !inQuotes) {
        inQuotes = true;
        quoteChar = char;
      } else if (char === quoteChar && inQuotes) {
        inQuotes = false;
        quoteChar = '';
      } else if (char === ' ' && !inQuotes) {
        if (current) {
          parts.push(current);
          current = '';
        }
      } else {
        current += char;
      }
    }

    if (current) {
      parts.push(current);
    }

    return parts;
  }
}

// Export singleton instance
export const commandProcessor = new CommandProcessor();
