import React, { useState } from 'react';
import { Box, Text } from 'ink';
import { inkColors, selectionColors } from '../theme.js';

// Configuration types
export type ConfigType = 'datasources' | 'models' | 'skills' | 'mcp' | 'kb';

export interface ConfigItem {
  id: string;
  name: string;
  type: string;
  status: 'connected' | 'disconnected' | 'error' | 'unknown';
  active: boolean;
  details: {
    address?: string;
    provider?: string;
    endpoint?: string;
    version?: string;
    description?: string;
    [key: string]: string | number | boolean | undefined;
  };
}

export interface ConfigViewProps {
  datasources: ConfigItem[];
  models: ConfigItem[];
  skills: ConfigItem[];
  mcp: ConfigItem[];
  kb: ConfigItem[];
  onClose?: () => void;
}

const TAB_NAMES: Record<ConfigType, string> = {
  datasources: 'Datasources',
  models: 'Models',
  skills: 'Skills',
  mcp: 'MCP',
  kb: 'KB',
};

const TAB_COMMANDS: Record<ConfigType, string[]> = {
  datasources: [
    'list datasources - List all datasources',
    'connect <id> - Connect to datasource',
    'disconnect <id> - Disconnect from datasource',
    'info <id> - Show datasource details',
  ],
  models: [
    'list models - List all models',
    'set model <id> - Set active model',
    'info model <id> - Show model details',
    'test model <id> - Test model connection',
  ],
  skills: [
    'list skills - List all skills',
    'enable skill <id> - Enable a skill',
    'disable skill <id> - Disable a skill',
    'info skill <id> - Show skill details',
  ],
  mcp: [
    'list mcp - List all MCP servers',
    'start mcp <id> - Start MCP server',
    'stop mcp <id> - Stop MCP server',
    'info mcp <id> - Show MCP server details',
  ],
  kb: [
    'list kb - List all knowledge bases',
    'load kb <id> - Load knowledge base',
    'unload kb <id> - Unload knowledge base',
    'info kb <id> - Show knowledge base details',
  ],
};

export const ConfigView: React.FC<ConfigViewProps> = ({
  datasources,
  models,
  skills,
  mcp,
  kb,
  onClose,
}) => {
  const [activeTab, setActiveTab] = useState<ConfigType>('datasources');

  // Get config items for the active tab
  const getConfigItems = (): ConfigItem[] => {
    switch (activeTab) {
      case 'datasources':
        return datasources;
      case 'models':
        return models;
      case 'skills':
        return skills;
      case 'mcp':
        return mcp;
      case 'kb':
        return kb;
      default:
        return [];
    }
  };

  // Get status icon
  const getStatusIcon = (status: ConfigItem['status']): string => {
    switch (status) {
      case 'connected':
        return '✓';
      case 'disconnected':
        return '○';
      case 'error':
        return '✖';
      case 'unknown':
        return '?';
      default:
        return '○';
    }
  };

  // Get status color
  const getStatusColor = (status: ConfigItem['status']): string => {
    switch (status) {
      case 'connected':
        return inkColors.success;
      case 'disconnected':
        return inkColors.muted;
      case 'error':
        return inkColors.error;
      case 'unknown':
        return inkColors.warning;
      default:
        return inkColors.muted;
    }
  };

  const configItems = getConfigItems();
  const commands = TAB_COMMANDS[activeTab];

  return (
    <Box flexDirection="column" height="100%" borderStyle="round" borderColor={inkColors.border} padding={1}>
      {/* Header */}
      <Box marginBottom={1}>
        <Text bold color={inkColors.accent}>
          Configuration Panel
        </Text>
        {onClose && (
          <Text dimColor> (Press ESC to close)</Text>
        )}
      </Box>

      {/* Tab Navigation */}
      <Box marginBottom={1} gap={1}>
        {(Object.keys(TAB_NAMES) as ConfigType[]).map((tabKey) => {
          const isActive = activeTab === tabKey;
          return (
            <Box key={tabKey} marginRight={1}>
              <Text
                bold={isActive}
                color={isActive ? selectionColors.accent : selectionColors.description}
                underline={isActive}
              >
                [{TAB_NAMES[tabKey]}]
              </Text>
            </Box>
          );
        })}
      </Box>

      {/* Config List */}
      <Box flexDirection="column" marginBottom={1} flexGrow={1}>
        <Box marginBottom={1}>
          <Text bold underline>
            {TAB_NAMES[activeTab]} Configuration
          </Text>
        </Box>

        {configItems.length === 0 ? (
          <Box>
            <Text dimColor>No {TAB_NAMES[activeTab].toLowerCase()} configured</Text>
          </Box>
        ) : (
          <Box flexDirection="column">
            {configItems.map((item) => (
              <Box key={item.id} flexDirection="column" marginBottom={1}>
                {/* Config item header */}
                <Box>
                  {/* Active indicator */}
                  <Text color={item.active ? selectionColors.accent : selectionColors.disabled}>
                    {item.active ? '● ' : '○ '}
                  </Text>

                  {/* Status icon */}
                  <Text color={getStatusColor(item.status)}>
                    {getStatusIcon(item.status)}{' '}
                  </Text>

                  {/* Name */}
                  <Text bold color={item.active ? selectionColors.selectedTitle : selectionColors.title}>
                    {item.name}
                  </Text>

                  {/* Type */}
                  <Text dimColor> ({item.type})</Text>
                </Box>

                {/* Config item details */}
                <Box paddingLeft={4} flexDirection="column">
                  {item.details.address && (
                    <Text dimColor>
                      Address: {item.details.address}
                    </Text>
                  )}
                  {item.details.provider && (
                    <Text dimColor>
                      Provider: {item.details.provider}
                    </Text>
                  )}
                  {item.details.endpoint && (
                    <Text dimColor>
                      Endpoint: {item.details.endpoint}
                    </Text>
                  )}
                  {item.details.version && (
                    <Text dimColor>
                      Version: {item.details.version}
                    </Text>
                  )}
                  {item.details.description && (
                    <Text dimColor>
                      {item.details.description}
                    </Text>
                  )}
                </Box>
              </Box>
            ))}
          </Box>
        )}
      </Box>

      {/* Available Commands */}
      <Box flexDirection="column" borderStyle="single" borderColor={inkColors.border} padding={1}>
        <Box marginBottom={1}>
          <Text bold underline>
            Available Commands
          </Text>
        </Box>
        {commands.map((command, index) => (
          <Text key={index} dimColor>
            {command}
          </Text>
        ))}
      </Box>

      {/* Footer hint */}
      <Box marginTop={1}>
        <Text dimColor>
          Use Tab/Shift+Tab to switch tabs, or type commands to manage configurations
        </Text>
      </Box>
    </Box>
  );
};
