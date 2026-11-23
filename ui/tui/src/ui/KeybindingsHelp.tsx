import React from 'react';
import { Box, Text } from 'ink';
import { KEYBINDINGS, type KeybindingAction } from './keybindings.js';
import { inkColors } from './theme.js';

interface KeybindingsHelpProps {
  compact?: boolean;
}

/**
 * Display keyboard shortcuts help
 */
export const KeybindingsHelp: React.FC<KeybindingsHelpProps> = ({ compact = false }) => {
  // Group keybindings by category
  const groupedKeybindings = KEYBINDINGS.reduce((acc, kb) => {
    if (!acc[kb.category]) {
      acc[kb.category] = [];
    }
    acc[kb.category].push(kb);
    return acc;
  }, {} as Record<KeybindingAction['category'], KeybindingAction[]>);

  const categoryTitles: Record<KeybindingAction['category'], string> = {
    system: 'System',
    navigation: 'Navigation',
    session: 'Session',
    input: 'Input',
  };

  const categoryColors: Record<KeybindingAction['category'], string> = {
    system: inkColors.error,
    navigation: inkColors.accent,
    session: inkColors.warning,
    input: inkColors.success,
  };

  if (compact) {
    return (
      <Box flexDirection="column">
        <Text bold color={inkColors.accent}>Keyboard Shortcuts</Text>
        <Text> </Text>
        {Object.entries(groupedKeybindings).map(([category, bindings]) => (
          <Box key={category} flexDirection="column" marginBottom={1}>
            <Text bold color={categoryColors[category as KeybindingAction['category']]}>
              {categoryTitles[category as KeybindingAction['category']]}:
            </Text>
            {bindings.map((kb, idx) => (
              <Box key={idx} paddingLeft={2}>
                <Text color={inkColors.text} bold>
                  {kb.key}
                </Text>
                <Text dimColor> - {kb.description}</Text>
              </Box>
            ))}
          </Box>
        ))}
      </Box>
    );
  }

  return (
    <Box flexDirection="column" paddingX={2}>
      <Box
        borderStyle="round"
        borderColor={inkColors.border}
        paddingX={2}
        paddingY={1}
        flexDirection="column"
      >
        <Text bold color={inkColors.accent} underline>
          Keyboard Shortcuts Reference
        </Text>
        <Text> </Text>

        {Object.entries(groupedKeybindings).map(([category, bindings]) => (
          <Box key={category} flexDirection="column" marginBottom={1}>
            <Text bold color={categoryColors[category as KeybindingAction['category']]}>
              {categoryTitles[category as KeybindingAction['category']]}
            </Text>
            {bindings.map((kb, idx) => (
              <Box key={idx} paddingLeft={2} justifyContent="space-between">
                <Box width={20}>
                  <Text color={inkColors.text} bold>
                    {kb.key}
                  </Text>
                </Box>
                <Box flexGrow={1}>
                  <Text dimColor>{kb.description}</Text>
                </Box>
              </Box>
            ))}
            {category !== 'input' && <Text> </Text>}
          </Box>
        ))}

        <Text> </Text>
        <Text dimColor italic>
          Press Ctrl+C to clear input, then press again to exit
        </Text>
      </Box>
    </Box>
  );
};

/**
 * Mini shortcuts indicator for status bar
 */
export const ShortcutsIndicator: React.FC = () => {
  const shortcuts = [
    { key: 'Tab', desc: 'Complete' },
    { key: '↑/↓', desc: 'History' },
    { key: 'Ctrl+N', desc: 'New' },
    { key: 'Ctrl+L', desc: 'Clear' },
  ];

  return (
    <Box>
      {shortcuts.map((s, idx) => (
        <React.Fragment key={idx}>
          {idx > 0 && <Text dimColor> | </Text>}
          <Text bold color={inkColors.accent}>
            {s.key}
          </Text>
          <Text dimColor>: {s.desc}</Text>
        </React.Fragment>
      ))}
    </Box>
  );
};
