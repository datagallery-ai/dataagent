import React, { useState, useEffect, useRef } from 'react';
import { Box, Text, useInput, useStdin } from 'ink';
import { isMouseInput } from '../input/mouse-wheel.js';
import { CommandHistory, CommandCompletion, DEFAULT_COMMANDS } from './keybindings.js';
import { inkColors } from './theme.js';
import { SlashCommandPopover, filterSlashCommands } from './SlashCommandPopover.js';
import { commandProcessor } from '../commands/CommandProcessor.js';

interface InputBoxProps {
  value?: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  disabled?: boolean;
  commands?: string[];
  placeholder?: string | undefined;
  modelName?: string | undefined;
  datasourceId?: string | undefined;
  skillId?: string | undefined;
  onExitRequest?: (clearInputDraft: () => boolean) => void;
  ctrlCExitPending?: boolean | undefined;
  outputCount?: number | undefined;
}

export const InputBox: React.FC<InputBoxProps> = ({
  value,
  onChange,
  onSubmit,
  disabled = false,
  commands = DEFAULT_COMMANDS,
  placeholder = 'Ask about your data... "Show tables"',
  modelName,
  datasourceId,
  skillId,
  onExitRequest,
  ctrlCExitPending = false,
  outputCount = 0,
}) => {
  const [localValue, setLocalValue] = useState('');
  const [completionHint, setCompletionHint] = useState<string>('');
  const [showSlashPopover, setShowSlashPopover] = useState(false);
  const [slashPopoverActiveIndex, setSlashPopoverActiveIndex] = useState(0);
  const { isRawModeSupported } = useStdin();
  const accent = disabled ? inkColors.muted : inkColors.accent;
  const metaParts = [
    datasourceId || 'no datasource',
    skillId,
  ].filter((part): part is string => Boolean(part));

  // Use refs to maintain history and completion across renders
  const historyRef = useRef(new CommandHistory());
  const completionRef = useRef(new CommandCompletion(commands));

  const availableCommands = React.useMemo(() => commandProcessor.getCommands(), []);

  const inputIsActive = Boolean(
    isRawModeSupported &&
      typeof process.stdin.setRawMode === 'function',
  );

  // Update completion commands when they change
  useEffect(() => {
    completionRef.current.setCommands(commands);
  }, [commands]);

  useEffect(() => {
    if (/^\/[^\s]*$/u.test(localValue) && !disabled) {
      setShowSlashPopover(true);
      setSlashPopoverActiveIndex(0);
    } else {
      setShowSlashPopover(false);
    }
  }, [localValue, disabled]);

  const getSlashFilter = (): string => {
    return localValue.match(/^\/([^\s]*)$/u)?.[1] ?? '';
  };

  const getFilteredCommands = () => filterSlashCommands(availableCommands, getSlashFilter());

  // Handle slash command selection
  const selectSlashCommand = (index: number) => {
    const filtered = getFilteredCommands();
    const cmd = filtered[index];
    if (cmd) {
      setLocalValue(`/${cmd.name} `);
      setShowSlashPopover(false);
      setCompletionHint('');
    }
  };

  const submitValue = (submittedValue: string) => {
    if (!submittedValue || (disabled && !submittedValue.startsWith('/'))) return;
    historyRef.current.add(submittedValue);
    onSubmit(submittedValue);
    setLocalValue('');
    onChange('');
    setCompletionHint('');
    setShowSlashPopover(false);
    setSlashPopoverActiveIndex(0);
    historyRef.current.reset();
    completionRef.current.reset();
  };

  // Handle keyboard input
  useInput(
    (input, key) => {
      if (isMouseInput(input)) {
        return;
      }

      if (key.escape && showSlashPopover) {
        setShowSlashPopover(false);
        setSlashPopoverActiveIndex(0);
        return;
      }

      if (showSlashPopover) {
        const filtered = getFilteredCommands();

        if (key.upArrow) {
          if (filtered.length > 0) {
            setSlashPopoverActiveIndex((previous) => Math.max(0, previous - 1));
          }
          return;
        }

        if (key.downArrow) {
          if (filtered.length > 0) {
            setSlashPopoverActiveIndex((previous) => (
              Math.min(filtered.length - 1, previous + 1)
            ));
          }
          return;
        }
      }

      // Handle Enter key
      if (key.return) {
        if (showSlashPopover) {
          const command = getFilteredCommands()[slashPopoverActiveIndex];
          if (command) {
            submitValue(`/${command.name}`);
            return;
          }
          setShowSlashPopover(false);
        }

        const submittedValue = localValue.trim();
        if (submittedValue) {
          submitValue(submittedValue);
        }
        return;
      }

      // Handle backspace
      if (key.backspace || key.delete) {
        const newValue = localValue.slice(0, -1);
        setLocalValue(newValue);
        setCompletionHint('');
        completionRef.current.reset();
        return;
      }

      // Handle up arrow - previous command in history (only when popover is closed)
      if (key.upArrow && !showSlashPopover) {
        const prevCommand = historyRef.current.previous(localValue);
        if (prevCommand !== null) {
          setLocalValue(prevCommand);
          setCompletionHint('');
          completionRef.current.reset();
        }
        return;
      }

      // Handle down arrow - next command in history (only when popover is closed)
      if (key.downArrow && !showSlashPopover) {
        const nextCommand = historyRef.current.next();
        if (nextCommand !== null) {
          setLocalValue(nextCommand);
          setCompletionHint('');
          completionRef.current.reset();
        }
        return;
      }

      if (key.tab && showSlashPopover) {
        if (getFilteredCommands().length > 0) {
          selectSlashCommand(slashPopoverActiveIndex);
        } else {
          setShowSlashPopover(false);
        }
        return;
      }

      // Handle Tab - command completion
      if (key.tab) {
        const completion = completionRef.current.complete(localValue);
        if (completion !== null) {
          setLocalValue(completion);
          setCompletionHint('');
        } else {
          // Show available completions as hint
          const completions = completionRef.current.getCompletions(localValue);
          if (completions.length > 0) {
            setCompletionHint(completions.join(', '));
          }
        }
        return;
      }

      // Ctrl+C follows qwen-code behavior: clear a draft first, then let the
      // application-level double-press exit state decide whether to quit.
      if (key.ctrl && input === 'c') {
        if (onExitRequest) {
          onExitRequest(() => {
            if (localValue.length === 0) {
              return false;
            }
            setLocalValue('');
            onChange('');
            setCompletionHint('');
            completionRef.current.reset();
            return true;
          });
        } else if (localValue.length > 0) {
          setLocalValue('');
          onChange('');
          setCompletionHint('');
          completionRef.current.reset();
        }
        return;
      }

      // Handle Ctrl+U - clear input
      if (key.ctrl && input === 'u') {
        setLocalValue('');
        onChange('');
        setCompletionHint('');
        completionRef.current.reset();
        return;
      }

      // Handle Ctrl+W - delete word
      if (key.ctrl && input === 'w') {
        const words = localValue.trimEnd().split(' ');
        words.pop();
        const newValue = words.join(' ');
        setLocalValue(newValue);
        setCompletionHint('');
        completionRef.current.reset();
        return;
      }

      // Ignore other control keys
      if (key.ctrl || key.meta || key.escape) {
        return;
      }

      // Add character to input
      const newValue = localValue + input;
      setLocalValue(newValue);

      // Update completion hint (only when popover is closed)
      if (!showSlashPopover) {
        const completions = completionRef.current.getCompletions(newValue);
        if (completions.length > 0) {
          setCompletionHint(`Tab: ${completions.slice(0, 3).join(', ')}${completions.length > 3 ? '...' : ''}`);
        } else {
          setCompletionHint('');
        }
      }

      completionRef.current.reset();
    },
    { isActive: inputIsActive }
  );

  return (
    <Box flexDirection="column" flexShrink={0} minHeight={4} position="relative">
      {showSlashPopover && !disabled && (
        <Box position="absolute" left={0} right={0} bottom="100%">
          <SlashCommandPopover
            commands={getFilteredCommands()}
            activeIndex={slashPopoverActiveIndex}
          />
        </Box>
      )}

      <Box flexDirection="row" width="100%">
        <Text color={accent} bold>
          {disabled ? '│' : '┃'}
        </Text>
        <Box
          flexDirection="column"
          flexGrow={1}
          paddingLeft={1}
          paddingRight={2}
          paddingY={1}
        >
          <Box minHeight={1}>
            <Text color={disabled ? inkColors.muted : inkColors.text} wrap="truncate-end">
              {localValue}
              {!disabled && <Text inverse> </Text>}
              {!localValue && <Text color={inkColors.muted}>{placeholder}</Text>}
            </Text>
          </Box>

          {!disabled && completionHint && !showSlashPopover && (
            <Box paddingTop={1}>
              <Text dimColor color={inkColors.accent} wrap="truncate-end">
                {completionHint}
              </Text>
            </Box>
          )}

          <Box flexDirection="row" justifyContent="space-between" paddingTop={1}>
            <Text wrap="truncate-end">
              <Text color={accent}>Analyze</Text>
              <Text dimColor> · </Text>
              <Text color={disabled ? inkColors.muted : inkColors.text}>
                {metaParts.join(' · ')}
              </Text>
            </Text>
            {ctrlCExitPending ? (
              <Text color={inkColors.warning} wrap="truncate-end">
                Press Ctrl+C again to exit.
              </Text>
            ) : (
              <Text>
                {outputCount > 0 && (
                  <>
                    <Text color={inkColors.accent}>Outputs {outputCount}</Text>
                    <Text dimColor> · </Text>
                  </>
                )}
                <Text color={inkColors.text}>Enter</Text>
                <Text dimColor> send</Text>
              </Text>
            )}
          </Box>
        </Box>
      </Box>
    </Box>
  );
};
