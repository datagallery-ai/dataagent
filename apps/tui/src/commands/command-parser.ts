/**
 * Command Parser Module
 *
 * Provides command parsing, validation, registration, and help generation
 * for the DataFoundry TUI command system.
 */

import { z } from 'zod';

/**
 * Parsed command argument types
 */
export interface ParsedArguments {
  /** Positional arguments */
  positional: string[];
  /** Flag-based arguments (--key value or --key=value) */
  flags: Record<string, string | boolean>;
  /** Raw unparsed arguments */
  raw: string[];
}

/**
 * Parameter definition for command validation
 */
export interface ParameterDefinition {
  /** Parameter name */
  name: string;
  /** Parameter description for help text */
  description: string;
  /** Whether parameter is required */
  required?: boolean;
  /** Parameter type */
  type?: 'string' | 'number' | 'boolean';
  /** Default value if not provided */
  default?: string | number | boolean;
  /** Validation schema (Zod) */
  schema?: z.ZodType<any>;
  /** Valid choices for the parameter */
  choices?: string[];
  /** Alias names for flags */
  alias?: string[];
}

/**
 * Command execution context
 */
export interface CommandContext {
  /** Parsed arguments */
  args: ParsedArguments;
  /** Original command string */
  rawCommand: string;
  /** Command name used (may be alias) */
  commandName: string;
}

/**
 * Command handler function
 */
export type CommandHandler = (context: CommandContext) => Promise<void> | void;

/**
 * Command definition
 */
export interface CommandDefinition {
  /** Primary command name */
  name: string;
  /** Command description */
  description: string;
  /** Usage example */
  usage: string;
  /** Command aliases */
  aliases?: string[];
  /** Positional parameters */
  parameters?: ParameterDefinition[];
  /** Flag parameters */
  flags?: ParameterDefinition[];
  /** Command handler */
  handler: CommandHandler;
  /** Whether command is hidden from help */
  hidden?: boolean;
  /** Command category for help grouping */
  category?: string;
  /** Examples of command usage */
  examples?: string[];
}

/**
 * Command validation error
 */
export class CommandValidationError extends Error {
  constructor(
    message: string,
    public parameterName?: string,
    public value?: any
  ) {
    super(message);
    this.name = 'CommandValidationError';
  }
}

/**
 * Command not found error
 */
export class CommandNotFoundError extends Error {
  constructor(
    public commandName: string
  ) {
    super(`Command not found: ${commandName}`);
    this.name = 'CommandNotFoundError';
  }
}

/**
 * Parse raw command input into arguments
 *
 * Supports:
 * - Positional arguments: cmd arg1 arg2
 * - Flag arguments: cmd --flag value, cmd --flag=value
 * - Boolean flags: cmd --flag
 * - Short flags: cmd -f value
 * - Quoted strings: cmd "arg with spaces"
 */
export function parseCommand(input: string): ParsedArguments {
  const raw = tokenizeCommand(input);
  const positional: string[] = [];
  const flags: Record<string, string | boolean> = {};

  let i = 0;
  while (i < raw.length) {
    const token = raw[i];

    // Long flag: --key or --key=value
    if (token.startsWith('--')) {
      const equalIndex = token.indexOf('=');

      if (equalIndex !== -1) {
        // --key=value format
        const key = token.slice(2, equalIndex);
        const value = token.slice(equalIndex + 1);
        flags[key] = value;
        i++;
      } else {
        // --key format, check next token
        const key = token.slice(2);

        if (i + 1 < raw.length && !raw[i + 1].startsWith('-')) {
          // Next token is the value
          flags[key] = raw[i + 1];
          i += 2;
        } else {
          // Boolean flag
          flags[key] = true;
          i++;
        }
      }
    }
    // Short flag: -f
    else if (token.startsWith('-') && token.length > 1 && token !== '-') {
      const key = token.slice(1);

      if (i + 1 < raw.length && !raw[i + 1].startsWith('-')) {
        // Next token is the value
        flags[key] = raw[i + 1];
        i += 2;
      } else {
        // Boolean flag
        flags[key] = true;
        i++;
      }
    }
    // Positional argument
    else {
      positional.push(token);
      i++;
    }
  }

  return { positional, flags, raw };
}

/**
 * Tokenize command string, respecting quotes
 */
function tokenizeCommand(input: string): string[] {
  const tokens: string[] = [];
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
        tokens.push(current);
        current = '';
      }
    } else {
      current += char;
    }
  }

  if (current) {
    tokens.push(current);
  }

  return tokens;
}

/**
 * Validate parsed arguments against command definition
 */
export function validateArguments(
  args: ParsedArguments,
  definition: CommandDefinition
): void {
  // Validate positional parameters
  if (definition.parameters) {
    const requiredParams = definition.parameters.filter(p => p.required);

    if (args.positional.length < requiredParams.length) {
      const missing = requiredParams[args.positional.length];
      throw new CommandValidationError(
        `Missing required parameter: ${missing.name}`,
        missing.name
      );
    }

    // Validate each positional parameter
    definition.parameters.forEach((param, index) => {
      const value = args.positional[index];

      if (value === undefined) {
        if (param.required) {
          throw new CommandValidationError(
            `Missing required parameter: ${param.name}`,
            param.name
          );
        }
        return;
      }

      validateParameterValue(value, param);
    });
  }

  // Validate flag parameters
  if (definition.flags) {
    definition.flags.forEach(flag => {
      // Check all possible names (main name + aliases)
      const allNames = [flag.name, ...(flag.alias || [])];
      let value: string | boolean | undefined;
      let foundName: string | undefined;

      for (const name of allNames) {
        if (name in args.flags) {
          value = args.flags[name];
          foundName = name;
          break;
        }
      }

      if (value === undefined) {
        if (flag.required) {
          throw new CommandValidationError(
            `Missing required flag: --${flag.name}`,
            flag.name
          );
        }
        return;
      }

      validateParameterValue(value, flag);
    });
  }
}

/**
 * Validate a single parameter value
 */
function validateParameterValue(
  value: string | boolean,
  param: ParameterDefinition
): void {
  // Type validation
  if (param.type === 'number' && typeof value === 'string') {
    const num = Number(value);
    if (isNaN(num)) {
      throw new CommandValidationError(
        `Parameter '${param.name}' must be a number`,
        param.name,
        value
      );
    }
  }

  if (param.type === 'boolean' && typeof value === 'string') {
    const lower = value.toLowerCase();
    if (!['true', 'false', 'yes', 'no', '1', '0'].includes(lower)) {
      throw new CommandValidationError(
        `Parameter '${param.name}' must be a boolean (true/false)`,
        param.name,
        value
      );
    }
  }

  // Choices validation
  if (param.choices && param.choices.length > 0 && typeof value === 'string') {
    if (!param.choices.includes(value)) {
      throw new CommandValidationError(
        `Parameter '${param.name}' must be one of: ${param.choices.join(', ')}`,
        param.name,
        value
      );
    }
  }

  // Zod schema validation
  if (param.schema) {
    const result = param.schema.safeParse(value);
    if (!result.success) {
      throw new CommandValidationError(
        `Parameter '${param.name}' validation failed: ${result.error.message}`,
        param.name,
        value
      );
    }
  }
}

/**
 * Command Registry
 *
 * Manages registration and lookup of commands
 */
export class CommandRegistry {
  private commands = new Map<string, CommandDefinition>();
  private aliases = new Map<string, string>();

  /**
   * Register a command
   */
  register(definition: CommandDefinition): void {
    // Register main command name
    this.commands.set(definition.name, definition);

    // Register aliases
    if (definition.aliases) {
      definition.aliases.forEach(alias => {
        this.aliases.set(alias, definition.name);
      });
    }
  }

  /**
   * Register multiple commands
   */
  registerAll(definitions: CommandDefinition[]): void {
    definitions.forEach(def => this.register(def));
  }

  /**
   * Find command by name or alias
   */
  find(nameOrAlias: string): CommandDefinition | undefined {
    // Try direct lookup
    let definition = this.commands.get(nameOrAlias);

    if (!definition) {
      // Try alias lookup
      const actualName = this.aliases.get(nameOrAlias);
      if (actualName) {
        definition = this.commands.get(actualName);
      }
    }

    return definition;
  }

  /**
   * Get command by name or alias, throws if not found
   */
  get(nameOrAlias: string): CommandDefinition {
    const definition = this.find(nameOrAlias);

    if (!definition) {
      throw new CommandNotFoundError(nameOrAlias);
    }

    return definition;
  }

  /**
   * Check if command exists
   */
  has(nameOrAlias: string): boolean {
    return this.find(nameOrAlias) !== undefined;
  }

  /**
   * Get all registered commands
   */
  all(): CommandDefinition[] {
    return Array.from(this.commands.values());
  }

  /**
   * Get all command names (including aliases)
   */
  allNames(): string[] {
    const names = Array.from(this.commands.keys());
    const aliasNames = Array.from(this.aliases.keys());
    return [...names, ...aliasNames];
  }

  /**
   * Clear all registered commands
   */
  clear(): void {
    this.commands.clear();
    this.aliases.clear();
  }

  /**
   * Unregister a command
   */
  unregister(name: string): boolean {
    const definition = this.commands.get(name);

    if (!definition) {
      return false;
    }

    // Remove aliases
    if (definition.aliases) {
      definition.aliases.forEach(alias => {
        this.aliases.delete(alias);
      });
    }

    return this.commands.delete(name);
  }
}

/**
 * Command Help Generator
 */
export class CommandHelpGenerator {
  constructor(private registry: CommandRegistry) {}

  /**
   * Generate help text for a specific command
   */
  generateCommandHelp(nameOrAlias: string): string {
    const definition = this.registry.get(nameOrAlias);
    const lines: string[] = [];

    // Command name and description
    lines.push(`Command: ${definition.name}`);

    if (definition.aliases && definition.aliases.length > 0) {
      lines.push(`Aliases: ${definition.aliases.join(', ')}`);
    }

    lines.push('');
    lines.push(definition.description);
    lines.push('');

    // Usage
    lines.push('Usage:');
    lines.push(`  ${definition.usage}`);
    lines.push('');

    // Parameters
    if (definition.parameters && definition.parameters.length > 0) {
      lines.push('Parameters:');
      definition.parameters.forEach(param => {
        const required = param.required ? '(required)' : '(optional)';
        const defaultValue = param.default !== undefined ? ` [default: ${param.default}]` : '';
        const choices = param.choices ? ` [choices: ${param.choices.join(', ')}]` : '';
        lines.push(`  ${param.name} ${required}${defaultValue}${choices}`);
        lines.push(`    ${param.description}`);
      });
      lines.push('');
    }

    // Flags
    if (definition.flags && definition.flags.length > 0) {
      lines.push('Flags:');
      definition.flags.forEach(flag => {
        const required = flag.required ? '(required)' : '(optional)';
        const aliases = flag.alias ? `, -${flag.alias.join(', -')}` : '';
        const defaultValue = flag.default !== undefined ? ` [default: ${flag.default}]` : '';
        const choices = flag.choices ? ` [choices: ${flag.choices.join(', ')}]` : '';
        lines.push(`  --${flag.name}${aliases} ${required}${defaultValue}${choices}`);
        lines.push(`    ${flag.description}`);
      });
      lines.push('');
    }

    // Examples
    if (definition.examples && definition.examples.length > 0) {
      lines.push('Examples:');
      definition.examples.forEach(example => {
        lines.push(`  ${example}`);
      });
      lines.push('');
    }

    return lines.join('\n');
  }

  /**
   * Generate help text for all commands
   */
  generateAllCommandsHelp(): string {
    const lines: string[] = [];
    const commands = this.registry.all().filter(cmd => !cmd.hidden);

    // Group by category
    const categorized = new Map<string, CommandDefinition[]>();

    commands.forEach(cmd => {
      const category = cmd.category || 'General';
      if (!categorized.has(category)) {
        categorized.set(category, []);
      }
      categorized.get(category)!.push(cmd);
    });

    lines.push('Available Commands:');
    lines.push('');

    // Sort categories
    const sortedCategories = Array.from(categorized.entries()).sort(([a], [b]) => {
      if (a === 'General') return -1;
      if (b === 'General') return 1;
      return a.localeCompare(b);
    });

    sortedCategories.forEach(([category, cmds]) => {
      lines.push(`${category}:`);

      cmds.forEach(cmd => {
        const aliases = cmd.aliases && cmd.aliases.length > 0
          ? ` (${cmd.aliases.join(', ')})`
          : '';
        lines.push(`  ${cmd.name}${aliases}`);
        lines.push(`    ${cmd.description}`);
      });

      lines.push('');
    });

    lines.push('Use "help <command>" for detailed information about a specific command.');

    return lines.join('\n');
  }

  /**
   * Generate short help summary
   */
  generateShortHelp(): string {
    const commands = this.registry.all()
      .filter(cmd => !cmd.hidden)
      .map(cmd => cmd.name)
      .sort();

    return `Available commands: ${commands.join(', ')}\nUse "help" for more information.`;
  }
}

/**
 * Default command registry instance
 */
export const defaultRegistry = new CommandRegistry();

/**
 * Default help generator instance
 */
export const defaultHelpGenerator = new CommandHelpGenerator(defaultRegistry);
