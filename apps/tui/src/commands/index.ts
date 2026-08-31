/**
 * Command system exports
 */

export { CommandProcessor, commandProcessor } from './CommandProcessor.js';
export { builtinCommands } from './builtinCommands.js';
export type { Command, CommandResult, CommandContext } from './types.js';

// Command parser exports
export {
  CommandRegistry,
  CommandHelpGenerator,
  defaultRegistry,
  defaultHelpGenerator,
  parseCommand,
  validateArguments,
  CommandValidationError,
  CommandNotFoundError,
} from './command-parser.js';

export type {
  CommandDefinition,
  ParameterDefinition,
  ParsedArguments,
  CommandHandler,
  CommandContext as ParserCommandContext,
} from './command-parser.js';

// Autocomplete exports
export {
  CommandAutocomplete,
  createDatasourceProvider,
  createTableProvider,
} from './autocomplete.js';

export type {
  CompletionSuggestion,
  CompletionValueProvider,
} from './autocomplete.js';
