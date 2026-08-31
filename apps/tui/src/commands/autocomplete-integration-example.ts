/**
 * Autocomplete Integration Example
 *
 * This file demonstrates how to integrate the CommandAutocomplete system
 * into the InputBox component and App.tsx
 */

import { CommandAutocomplete, createDatasourceProvider, createTableProvider } from './autocomplete.js';
import { defaultRegistry, CommandRegistry, CommandDefinition } from './command-parser.js';

/**
 * STEP 1: Initialize the autocomplete system
 * This should be done once when the app starts, typically in App.tsx
 */
export function initializeAutocomplete(): CommandAutocomplete {
  // Create autocomplete instance with the command registry
  const autocomplete = new CommandAutocomplete(defaultRegistry);

  // Register value providers for dynamic completions

  // Example: Datasource provider
  const datasourceProvider = createDatasourceProvider(async () => {
    // This should fetch from your actual state/store
    // For example: useStore.getState().datasources
    return [
      { id: 'postgres-prod', name: 'Production PostgreSQL' },
      { id: 'mysql-dev', name: 'Development MySQL' },
      { id: 'mongo-analytics', name: 'Analytics MongoDB' },
    ];
  });
  autocomplete.registerValueProvider(datasourceProvider);

  // Example: Table provider
  const tableProvider = createTableProvider(async () => {
    // This should fetch from your actual connected datasource
    return ['users', 'orders', 'products', 'customers', 'transactions'];
  });
  autocomplete.registerValueProvider(tableProvider);

  return autocomplete;
}

/**
 * STEP 2: Register commands with the registry
 * This should be done before initializing autocomplete
 */
export function registerExampleCommands(registry: CommandRegistry): void {
  // Example: connect command
  const connectCommand: CommandDefinition = {
    name: 'connect',
    description: 'Connect to a datasource',
    usage: 'connect <datasource-id>',
    category: 'Datasource',
    parameters: [
      {
        name: 'datasource-id',
        description: 'ID of the datasource to connect to',
        required: true,
        type: 'string',
      },
    ],
    flags: [
      {
        name: 'timeout',
        description: 'Connection timeout in seconds',
        type: 'number',
        alias: ['t'],
        default: 30,
      },
    ],
    handler: async (context) => {
      console.log('Connecting to datasource:', context.args.positional[0]);
    },
    examples: [
      'connect postgres-prod',
      'connect mysql-dev --timeout 60',
    ],
  };

  // Example: query command
  const queryCommand: CommandDefinition = {
    name: 'query',
    description: 'Execute a SQL query',
    usage: 'query <sql>',
    aliases: ['q'],
    category: 'Query',
    parameters: [
      {
        name: 'sql',
        description: 'SQL query to execute',
        required: true,
        type: 'string',
      },
    ],
    flags: [
      {
        name: 'format',
        description: 'Output format',
        choices: ['table', 'json', 'csv'],
        default: 'table',
        alias: ['f'],
      },
      {
        name: 'limit',
        description: 'Maximum number of rows to return',
        type: 'number',
        alias: ['l'],
      },
    ],
    handler: async (context) => {
      console.log('Executing query:', context.args.positional[0]);
    },
    examples: [
      'query "SELECT * FROM users"',
      'query "SELECT * FROM orders" --format json --limit 100',
    ],
  };

  // Example: describe command
  const describeCommand: CommandDefinition = {
    name: 'describe',
    description: 'Describe a table schema',
    usage: 'describe <table>',
    aliases: ['desc'],
    category: 'Schema',
    parameters: [
      {
        name: 'table',
        description: 'Table name to describe',
        required: true,
        type: 'string',
      },
    ],
    handler: async (context) => {
      console.log('Describing table:', context.args.positional[0]);
    },
    examples: [
      'describe users',
      'desc orders',
    ],
  };

  // Register all commands
  registry.register(connectCommand);
  registry.register(queryCommand);
  registry.register(describeCommand);
}

/**
 * STEP 3: Use autocomplete in InputBox component
 *
 * Here's how to modify the InputBox component to use the new autocomplete:
 */

/**
 * Example InputBox integration (pseudo-code):
 *
 * ```tsx
 * import { CommandAutocomplete } from '../commands/autocomplete.js';
 *
 * interface InputBoxProps {
 *   value: string;
 *   onChange: (value: string) => void;
 *   onSubmit: (value: string) => void;
 *   disabled?: boolean;
 *   autocomplete?: CommandAutocomplete; // Add this
 * }
 *
 * export const InputBox: React.FC<InputBoxProps> = ({
 *   value,
 *   onChange,
 *   onSubmit,
 *   disabled = false,
 *   autocomplete, // Add this
 * }) => {
 *   const [localValue, setLocalValue] = useState(value);
 *   const [completionHint, setCompletionHint] = useState<string>('');
 *   const [completionSuggestions, setCompletionSuggestions] = useState<CompletionSuggestion[]>([]);
 *
 *   // Handle keyboard input
 *   useInput(
 *     async (input, key) => {
 *       if (disabled) return;
 *
 *       // Handle Tab - command completion
 *       if (key.tab && autocomplete) {
 *         // Get single best completion
 *         const completion = await autocomplete.getCompletion(localValue);
 *         if (completion) {
 *           setLocalValue(completion);
 *           onChange(completion);
 *           setCompletionHint('');
 *         } else {
 *           // Show all available completions
 *           const suggestions = await autocomplete.getCompletions(localValue);
 *           if (suggestions.length > 0) {
 *             setCompletionSuggestions(suggestions);
 *             const hint = suggestions
 *               .slice(0, 5)
 *               .map(s => s.text)
 *               .join(', ');
 *             setCompletionHint(`Options: ${hint}${suggestions.length > 5 ? '...' : ''}`);
 *           }
 *         }
 *         return;
 *       }
 *
 *       // On regular input, update completion hints
 *       if (!key.ctrl && !key.meta && !key.escape && autocomplete) {
 *         const newValue = localValue + input;
 *         setLocalValue(newValue);
 *         onChange(newValue);
 *
 *         // Get completions asynchronously
 *         const suggestions = await autocomplete.getCompletions(newValue);
 *         if (suggestions.length > 0) {
 *           const hint = suggestions
 *             .slice(0, 3)
 *             .map(s => `${s.text} (${s.type})`)
 *             .join(', ');
 *           setCompletionHint(`Tab: ${hint}${suggestions.length > 3 ? '...' : ''}`);
 *         } else {
 *           setCompletionHint('');
 *         }
 *       }
 *     },
 *     { isActive: inputIsActive }
 *   );
 *
 *   // Render completion suggestions list
 *   return (
 *     <Box flexDirection="column">
 *       {completionSuggestions.length > 0 && (
 *         <Box flexDirection="column" borderStyle="round" borderColor="cyan" paddingX={1}>
 *           <Text color="cyan" bold>Suggestions:</Text>
 *           {completionSuggestions.slice(0, 10).map((suggestion, i) => (
 *             <Text key={i}>
 *               <Text color="yellow">{suggestion.text}</Text>
 *               {' - '}
 *               <Text dimColor>{suggestion.description || suggestion.type}</Text>
 *             </Text>
 *           ))}
 *         </Box>
 *       )}
 *
 *       <Box borderStyle="single" borderColor={disabled ? 'gray' : 'blue'} paddingX={1}>
 *         <Text bold color={disabled ? 'gray' : 'blue'}> {disabled ? '⊗' : '>'} </Text>
 *         <Text color={disabled ? 'gray' : 'white'}>
 *           {localValue}
 *           {!disabled && <Text inverse> </Text>}
 *         </Text>
 *       </Box>
 *
 *       {!disabled && completionHint && (
 *         <Box paddingLeft={2}>
 *           <Text dimColor color="cyan">{completionHint}</Text>
 *         </Box>
 *       )}
 *     </Box>
 *   );
 * };
 * ```
 */

/**
 * STEP 4: Wire it up in App.tsx
 *
 * ```tsx
 * import { initializeAutocomplete, registerExampleCommands } from './commands/autocomplete-integration-example.js';
 * import { defaultRegistry } from './commands/index.js';
 *
 * function App() {
 *   const [autocomplete] = useState(() => {
 *     // Register commands first
 *     registerExampleCommands(defaultRegistry);
 *
 *     // Then initialize autocomplete
 *     return initializeAutocomplete();
 *   });
 *
 *   return (
 *     <Box flexDirection="column">
 *       <Header />
 *       <ChatArea messages={messages} />
 *       <InputBox
 *         value={input}
 *         onChange={setInput}
 *         onSubmit={handleSubmit}
 *         disabled={loading}
 *         autocomplete={autocomplete}  // Pass autocomplete instance
 *       />
 *     </Box>
 *   );
 * }
 * ```
 */

/**
 * STEP 5: Advanced - Custom value providers
 *
 * You can create custom providers for any dynamic data:
 */
export function createCustomProviderExample(autocomplete: CommandAutocomplete) {
  // Example: Provider that gets data from Zustand store
  const storeProvider = {
    name: 'store-data',
    getValues: async (parameterName: string, commandName: string) => {
      // Access your store here
      // const state = useStore.getState();

      if (parameterName === 'session-id') {
        // Return available session IDs
        return ['session-1', 'session-2', 'session-3'];
      }

      if (parameterName === 'thread-id') {
        // Return available thread IDs
        return ['thread-abc', 'thread-def', 'thread-ghi'];
      }

      return [];
    },
  };

  autocomplete.registerValueProvider(storeProvider);
}
