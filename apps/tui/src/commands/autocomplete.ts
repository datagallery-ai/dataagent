/**
 * Command Autocomplete Module
 *
 * Provides intelligent autocomplete suggestions for commands, subcommands,
 * flags, and parameter values in the DataFoundry TUI.
 */

import type { CommandRegistry, CommandDefinition, ParameterDefinition } from './command-parser.js';
import { getLogger } from '../utils/logger.js';

/**
 * Autocomplete suggestion with metadata
 */
export interface CompletionSuggestion {
  /** The completion text to insert */
  text: string;
  /** Display label (may include additional info) */
  label: string;
  /** Type of completion */
  type: 'command' | 'subcommand' | 'flag' | 'parameter' | 'value';
  /** Optional description */
  description?: string;
  /** Priority for sorting (higher = more important) */
  priority?: number;
}

/**
 * Completion context parsed from current input
 */
interface CompletionContext {
  /** Full input string */
  fullInput: string;
  /** Tokens parsed from input */
  tokens: string[];
  /** Current token being typed (may be partial) */
  currentToken: string;
  /** Position of cursor in input */
  cursorPosition: number;
  /** Whether we're completing a command name */
  isCommandName: boolean;
  /** Whether we're completing a flag */
  isFlag: boolean;
  /** Whether we're completing a parameter value */
  isParameterValue: boolean;
  /** Command name if identified */
  commandName?: string | undefined;
  /** Command definition if found */
  command?: CommandDefinition | undefined;
}

/**
 * Provider for dynamic completion values (e.g., datasource IDs from state)
 */
export interface CompletionValueProvider {
  /** Provider name for identification */
  name: string;
  /** Get values for a specific parameter */
  getValues: (parameterName: string, commandName: string) => Promise<string[]> | string[];
}

/**
 * Autocomplete engine for command system
 */
export class CommandAutocomplete {
  private registry: CommandRegistry;
  private valueProviders: Map<string, CompletionValueProvider> = new Map();
  private cachedCompletions: Map<string, CompletionSuggestion[]> = new Map();
  private cacheTimeout = 5000; // 5 seconds

  constructor(registry: CommandRegistry) {
    this.registry = registry;
  }

  /**
   * Register a value provider for dynamic completions
   */
  registerValueProvider(provider: CompletionValueProvider): void {
    this.valueProviders.set(provider.name, provider);
  }

  /**
   * Unregister a value provider
   */
  unregisterValueProvider(name: string): void {
    this.valueProviders.delete(name);
  }

  /**
   * Get completion suggestions for current input
   *
   * @param input - Current input string
   * @param cursorPosition - Cursor position (defaults to end of input)
   * @returns Array of completion suggestions
   */
  async getCompletions(
    input: string,
    cursorPosition: number = input.length
  ): Promise<CompletionSuggestion[]> {
    // Check cache first
    const cacheKey = `${input}:${cursorPosition}`;
    const cached = this.cachedCompletions.get(cacheKey);
    if (cached) {
      return cached;
    }

    // Parse input context
    const context = this.parseContext(input, cursorPosition);

    // Get appropriate completions based on context
    let completions: CompletionSuggestion[] = [];

    if (context.isCommandName) {
      completions = this.getCommandCompletions(context);
    } else if (context.isFlag) {
      completions = this.getFlagCompletions(context);
    } else if (context.isParameterValue && context.command) {
      completions = await this.getParameterValueCompletions(context);
    } else if (context.command) {
      // General parameter/flag suggestions
      completions = [
        ...this.getFlagCompletions(context),
        ...await this.getParameterValueCompletions(context),
      ];
    }

    // Sort by priority and alphabetically
    completions.sort((a, b) => {
      const priorityDiff = (b.priority || 0) - (a.priority || 0);
      if (priorityDiff !== 0) return priorityDiff;
      return a.text.localeCompare(b.text);
    });

    // Cache results
    this.cacheCompletions(cacheKey, completions);

    return completions;
  }

  /**
   * Get the best single completion (for Tab key)
   *
   * @param input - Current input string
   * @returns Completed string, or null if no unique completion
   */
  async getCompletion(input: string): Promise<string | null> {
    const completions = await this.getCompletions(input);

    if (completions.length === 0) {
      return null;
    }

    // If there's exactly one completion, return it
    if (completions.length === 1) {
      return this.applyCompletion(input, completions[0]);
    }

    // If multiple completions share a common prefix, complete up to that prefix
    const commonPrefix = this.findCommonPrefix(completions.map(c => c.text));
    const context = this.parseContext(input, input.length);

    if (commonPrefix && commonPrefix.length > context.currentToken.length) {
      // Replace current token with common prefix
      const beforeToken = input.substring(0, input.length - context.currentToken.length);
      return beforeToken + commonPrefix;
    }

    return null;
  }

  /**
   * Apply a specific completion to the input
   */
  applyCompletion(input: string, suggestion: CompletionSuggestion): string {
    const context = this.parseContext(input, input.length);
    const beforeToken = input.substring(0, input.length - context.currentToken.length);

    // Add space after completion unless it's a flag that expects a value
    const needsSpace = suggestion.type !== 'flag' || suggestion.text.endsWith('=');

    return beforeToken + suggestion.text + (needsSpace ? ' ' : '');
  }

  /**
   * Parse input context for completion
   */
  private parseContext(input: string, cursorPosition: number): CompletionContext {
    // Get input up to cursor
    const inputToCursor = input.substring(0, cursorPosition);

    // Tokenize (simple space-based for now)
    const tokens = inputToCursor.trim().split(/\s+/).filter(t => t.length > 0);

    // Determine current token (the one being typed)
    const currentToken = inputToCursor.match(/\S+$/)?.[0] || '';

    // Determine if we're completing a command name (first token)
    const isCommandName = tokens.length <= 1;

    // Determine command name
    let commandName: string | undefined;
    let command: CommandDefinition | undefined;

    if (tokens.length > 0 && !isCommandName) {
      commandName = tokens[0];
      command = this.registry.find(commandName);
    } else if (tokens.length === 1 && !currentToken.startsWith('-')) {
      // Might be completing a command name
      commandName = tokens[0];
      command = this.registry.find(commandName);
    }

    // Determine if completing a flag
    const isFlag = currentToken.startsWith('-');

    // Determine if completing a parameter value
    const isParameterValue = !isFlag && !isCommandName && tokens.length > 1;

    return {
      fullInput: input,
      tokens,
      currentToken,
      cursorPosition,
      isCommandName,
      isFlag,
      isParameterValue,
      commandName,
      command,
    };
  }

  /**
   * Get command name completions
   */
  private getCommandCompletions(context: CompletionContext): CompletionSuggestion[] {
    const commands = this.registry.all().filter(cmd => !cmd.hidden);
    const prefix = context.currentToken.toLowerCase();

    return commands
      .filter(cmd => {
        // Match command name or aliases
        if (cmd.name.toLowerCase().startsWith(prefix)) return true;
        if (cmd.aliases?.some(alias => alias.toLowerCase().startsWith(prefix))) return true;
        return false;
      })
      .flatMap(cmd => {
        const suggestions: CompletionSuggestion[] = [];

        // Add main command
        if (cmd.name.toLowerCase().startsWith(prefix)) {
          suggestions.push({
            text: cmd.name,
            label: `${cmd.name} - ${cmd.description}`,
            type: 'command',
            description: cmd.description,
            priority: 10,
          });
        }

        // Add aliases
        if (cmd.aliases) {
          cmd.aliases
            .filter(alias => alias.toLowerCase().startsWith(prefix))
            .forEach(alias => {
              suggestions.push({
                text: alias,
                label: `${alias} (alias for ${cmd.name})`,
                type: 'command',
                description: `Alias for ${cmd.name}`,
                priority: 5,
              });
            });
        }

        return suggestions;
      });
  }

  /**
   * Get flag completions for current command
   */
  private getFlagCompletions(context: CompletionContext): CompletionSuggestion[] {
    if (!context.command?.flags) {
      return [];
    }

    const prefix = context.currentToken.replace(/^-+/, '').toLowerCase();
    const isLongFlag = context.currentToken.startsWith('--');

    return context.command.flags
      .filter(flag => {
        const flagName = flag.name.toLowerCase();
        return flagName.startsWith(prefix);
      })
      .flatMap(flag => {
        const suggestions: CompletionSuggestion[] = [];

        // Add long flag format
        if (isLongFlag || context.currentToken.startsWith('--')) {
          const flagText = `--${flag.name}`;
          suggestions.push({
            text: flagText,
            label: `${flagText} - ${flag.description}`,
            type: 'flag',
            description: flag.description,
            priority: flag.required ? 15 : 5,
          });
        }

        // Add short flag format (aliases)
        if (flag.alias && (!isLongFlag || context.currentToken === '-')) {
          flag.alias.forEach(alias => {
            if (alias.length === 1) {
              const flagText = `-${alias}`;
              suggestions.push({
                text: flagText,
                label: `${flagText} - ${flag.description}`,
                type: 'flag',
                description: flag.description,
                priority: flag.required ? 15 : 3,
              });
            }
          });
        }

        return suggestions;
      });
  }

  /**
   * Get parameter value completions
   */
  private async getParameterValueCompletions(
    context: CompletionContext
  ): Promise<CompletionSuggestion[]> {
    if (!context.command) {
      return [];
    }

    const suggestions: CompletionSuggestion[] = [];

    // Determine which parameter we're completing
    const parameter = this.identifyParameter(context);

    if (!parameter) {
      return [];
    }

    // If parameter has choices, suggest them
    if (parameter.choices && parameter.choices.length > 0) {
      const prefix = context.currentToken.toLowerCase();

      parameter.choices
        .filter(choice => choice.toLowerCase().startsWith(prefix))
        .forEach(choice => {
          suggestions.push({
            text: choice,
            label: choice,
            type: 'value',
            description: `Valid value for ${parameter.name}`,
            priority: 8,
          });
        });
    }

    // Get dynamic values from providers
    const dynamicValues = await this.getDynamicValues(
      parameter.name,
      context.commandName || ''
    );

    const prefix = context.currentToken.toLowerCase();
    dynamicValues
      .filter(value => value.toLowerCase().startsWith(prefix))
      .forEach(value => {
        suggestions.push({
          text: value,
          label: value,
          type: 'value',
          description: `Available ${parameter.name}`,
          priority: 7,
        });
      });

    return suggestions;
  }

  /**
   * Identify which parameter is being completed based on position
   */
  private identifyParameter(context: CompletionContext): ParameterDefinition | undefined {
    if (!context.command) {
      return undefined;
    }

    // Count positional arguments (non-flags)
    const positionalArgs = context.tokens.slice(1).filter(token => !token.startsWith('-'));
    const currentPositionIndex = positionalArgs.length - (context.isParameterValue ? 1 : 0);

    // Check if we're completing a flag value
    const previousToken = context.tokens[context.tokens.length - 2];
    if (previousToken?.startsWith('-')) {
      const flagName = previousToken.replace(/^-+/, '');
      const flag = context.command.flags?.find(f =>
        f.name === flagName || f.alias?.includes(flagName)
      );
      return flag;
    }

    // Otherwise, complete positional parameter
    if (context.command.parameters && currentPositionIndex < context.command.parameters.length) {
      return context.command.parameters[currentPositionIndex];
    }

    return undefined;
  }

  /**
   * Get dynamic values from registered providers
   */
  private async getDynamicValues(parameterName: string, commandName: string): Promise<string[]> {
    const allValues: string[] = [];

    for (const provider of this.valueProviders.values()) {
      try {
        const values = await provider.getValues(parameterName, commandName);
        allValues.push(...values);
      } catch (error) {
        getLogger().error(`Autocomplete provider ${provider.name} failed`, error);
      }
    }

    // Remove duplicates
    return Array.from(new Set(allValues));
  }

  /**
   * Find common prefix among strings
   */
  private findCommonPrefix(strings: string[]): string {
    if (strings.length === 0) return '';
    if (strings.length === 1) return strings[0];

    let prefix = strings[0];

    for (let i = 1; i < strings.length; i++) {
      while (!strings[i].startsWith(prefix)) {
        prefix = prefix.substring(0, prefix.length - 1);
        if (prefix === '') return '';
      }
    }

    return prefix;
  }

  /**
   * Cache completions with timeout
   */
  private cacheCompletions(key: string, completions: CompletionSuggestion[]): void {
    this.cachedCompletions.set(key, completions);

    // Clear cache after timeout
    setTimeout(() => {
      this.cachedCompletions.delete(key);
    }, this.cacheTimeout);
  }

  /**
   * Clear completion cache
   */
  clearCache(): void {
    this.cachedCompletions.clear();
  }
}

/**
 * Create a datasource ID provider
 * Example usage for providing available datasource IDs
 */
export function createDatasourceProvider(
  getDatasources: () => Promise<Array<{ id: string; name: string }>> | Array<{ id: string; name: string }>
): CompletionValueProvider {
  return {
    name: 'datasources',
    getValues: async (parameterName: string, commandName: string) => {
      // Only provide datasource IDs for relevant parameters
      if (
        parameterName.toLowerCase().includes('datasource') ||
        parameterName.toLowerCase().includes('source') ||
        commandName.toLowerCase().includes('connect')
      ) {
        const datasources = await getDatasources();
        return datasources.map(ds => ds.id);
      }
      return [];
    },
  };
}

/**
 * Create a table name provider
 * Example usage for providing available table names
 */
export function createTableProvider(
  getTables: () => Promise<string[]> | string[]
): CompletionValueProvider {
  return {
    name: 'tables',
    getValues: async (parameterName: string, commandName: string) => {
      // Only provide table names for relevant parameters
      if (
        parameterName.toLowerCase().includes('table') ||
        commandName.toLowerCase().includes('table') ||
        commandName.toLowerCase().includes('describe')
      ) {
        return await getTables();
      }
      return [];
    },
  };
}
