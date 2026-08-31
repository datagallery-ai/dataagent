import React from 'react';
import { Box, Text } from 'ink';
import { selectionColors } from './theme.js';

export interface SlashCommandItem {
  name: string;
  description?: string | undefined;
  aliases?: string[] | undefined;
}

interface SlashCommandPopoverProps {
  commands: SlashCommandItem[];
  activeIndex: number;
}

export const SLASH_COMMAND_POPOVER_ROWS = 8;

export function filterSlashCommands(
  commands: SlashCommandItem[],
  query: string,
): SlashCommandItem[] {
  const normalizedQuery = query.trim().toLowerCase();
  const alphabetized = [...commands].sort((left, right) => left.name.localeCompare(right.name));
  if (!normalizedQuery) return alphabetized;

  return alphabetized
    .map((command) => {
      const name = command.name.toLowerCase();
      const aliases = command.aliases?.map((alias) => alias.toLowerCase()) ?? [];
      const description = command.description?.toLowerCase() ?? '';
      const rank = name.startsWith(normalizedQuery)
        ? 0
        : aliases.some((alias) => alias.startsWith(normalizedQuery))
          ? 1
          : name.includes(normalizedQuery)
            ? 2
            : aliases.some((alias) => alias.includes(normalizedQuery))
              ? 3
              : description.includes(normalizedQuery)
                ? 4
                : -1;
      return { command, rank };
    })
    .filter((match) => match.rank >= 0)
    .sort((left, right) => left.rank - right.rank)
    .map((match) => match.command);
}

export function slashCommandPopoverRows(commandCount: number): number {
  return Math.max(1, Math.min(SLASH_COMMAND_POPOVER_ROWS, commandCount));
}

export const SlashCommandPopover: React.FC<SlashCommandPopoverProps> = ({
  commands,
  activeIndex,
}) => {
  if (commands.length === 0) {
    return (
      <Box
        height={2}
        width="100%"
        paddingLeft={1}
        borderStyle="single"
        borderTop
        borderBottom={false}
        borderLeft={false}
        borderRight={false}
        borderColor={selectionColors.border}
        backgroundColor={selectionColors.background}
      >
        <Text color={selectionColors.disabled} wrap="truncate-end">No matching commands</Text>
      </Box>
    );
  }

  const selectedIndex = Math.max(0, Math.min(activeIndex, commands.length - 1));
  const visibleRows = slashCommandPopoverRows(commands.length);
  const previewRows = Math.min(2, Math.floor((visibleRows - 1) / 2));
  const maxOffset = Math.max(0, commands.length - visibleRows);
  const offset = Math.max(
    0,
    Math.min(maxOffset, selectedIndex - visibleRows + previewRows + 1),
  );
  const visibleCommands = commands.slice(offset, offset + visibleRows);
  const commandColumnWidth = Math.min(
    28,
    Math.max(12, ...commands.map((command) => `/${command.name}`.length + 2)),
  );

  return (
    <Box
      flexDirection="column"
      height={visibleRows + 1}
      width="100%"
      overflowY="hidden"
      borderStyle="single"
      borderTop
      borderBottom={false}
      borderLeft={false}
      borderRight={false}
      borderColor={selectionColors.border}
      backgroundColor={selectionColors.background}
    >
      {visibleCommands.map((command, rowIndex) => {
        const isActive = offset + rowIndex === selectedIndex;
        return (
          <Box
            key={command.name}
            flexDirection="row"
            width="100%"
            height={1}
            paddingRight={1}
            backgroundColor={isActive
              ? selectionColors.selectedBackground
              : selectionColors.background}
          >
            <Box width={2} flexShrink={0}>
              {isActive
                ? <Text color={selectionColors.accent}>› </Text>
                : <Text>  </Text>}
            </Box>
            <Box width={1} flexShrink={0}>
              <Text color={selectionColors.accent}>/</Text>
            </Box>
            <Box width={commandColumnWidth - 1} flexShrink={0}>
              <Text
                color={isActive ? selectionColors.selectedTitle : selectionColors.title}
                bold={isActive}
                wrap="truncate-end"
              >
                {command.name}
              </Text>
            </Box>
            <Box flexGrow={1} minWidth={0}>
              <Text
                color={isActive
                  ? selectionColors.selectedDescription
                  : selectionColors.description}
                wrap="truncate-end"
              >
                {command.description ?? ''}
              </Text>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
};
