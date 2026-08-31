/**
 * 键盘快捷键系统集成示例
 * 演示如何使用 keybindings.ts 提供的功能
 */

import React, { useState, useRef, useEffect } from 'react';
import { Box, Text, useInput } from 'ink';
import {
  CommandHistory,
  CommandCompletion,
  DEFAULT_COMMANDS,
  getKeybindingsHelp,
  formatKeybinding,
  KEYBINDINGS
} from './keybindings.js';

// ============================================================================
// 示例 1: 基础输入框，带历史记录和补全
// ============================================================================

interface SimpleInputProps {
  onSubmit: (value: string) => void;
  suggestions?: string[];
}

export const SimpleInputWithHistory: React.FC<SimpleInputProps> = ({
  onSubmit,
  suggestions = DEFAULT_COMMANDS
}) => {
  const [input, setInput] = useState('');
  const [hint, setHint] = useState('');

  // 使用 ref 避免不必要的重渲染
  const historyRef = useRef(new CommandHistory(50));
  const completionRef = useRef(new CommandCompletion(suggestions));

  // 更新补全命令
  useEffect(() => {
    completionRef.current.setCommands(suggestions);
  }, [suggestions]);

  useInput((char, key) => {
    // Enter - 提交
    if (key.return) {
      if (input.trim()) {
        historyRef.current.add(input.trim());
        onSubmit(input.trim());
        setInput('');
        setHint('');
        historyRef.current.reset();
      }
      return;
    }

    // 上箭头 - 历史上一条
    if (key.upArrow) {
      const prev = historyRef.current.previous();
      if (prev !== null) {
        setInput(prev);
        setHint('');
      }
      return;
    }

    // 下箭头 - 历史下一条
    if (key.downArrow) {
      const next = historyRef.current.next();
      if (next !== null) {
        setInput(next);
        setHint('');
      }
      return;
    }

    // Tab - 命令补全
    if (key.tab) {
      const completion = completionRef.current.complete(input);
      if (completion) {
        setInput(completion);
        setHint('');
      } else {
        const matches = completionRef.current.getCompletions(input);
        if (matches.length > 0) {
          setHint(`Available: ${matches.join(', ')}`);
        }
      }
      return;
    }

    // Backspace - 删除字符
    if (key.backspace || key.delete) {
      const newInput = input.slice(0, -1);
      setInput(newInput);
      updateHint(newInput);
      return;
    }

    // Ctrl+U - 清空输入
    if (key.ctrl && char === 'u') {
      setInput('');
      setHint('');
      completionRef.current.reset();
      return;
    }

    // Ctrl+W - 删除单词
    if (key.ctrl && char === 'w') {
      const words = input.trimEnd().split(' ');
      words.pop();
      const newInput = words.join(' ');
      setInput(newInput);
      updateHint(newInput);
      return;
    }

    // 忽略其他控制键
    if (key.ctrl || key.meta || key.escape) {
      return;
    }

    // 添加字符
    const newInput = input + char;
    setInput(newInput);
    updateHint(newInput);
    completionRef.current.reset();
  });

  const updateHint = (value: string) => {
    const matches = completionRef.current.getCompletions(value);
    if (matches.length > 0) {
      setHint(`Tab: ${matches.slice(0, 3).join(', ')}${matches.length > 3 ? '...' : ''}`);
    } else {
      setHint('');
    }
  };

  return (
    <Box flexDirection="column" borderStyle="single" padding={1}>
      <Box>
        <Text color="cyan" bold>&gt; </Text>
        <Text>{input}</Text>
        <Text inverse> </Text>
      </Box>
      {hint && (
        <Box paddingLeft={2}>
          <Text dimColor color="yellow">{hint}</Text>
        </Box>
      )}
      <Box>
        <Text dimColor>↑/↓ History | Tab Complete | Enter Submit</Text>
      </Box>
    </Box>
  );
};

// ============================================================================
// 示例 2: 带焦点管理的多输入系统
// ============================================================================

interface MultiInputDemoProps {
  onCommand: (cmd: string) => void;
}

export const MultiInputDemo: React.FC<MultiInputDemoProps> = ({ onCommand }) => {
  const [activeInput, setActiveInput] = useState<'main' | 'search'>('main');
  const [mainInput, setMainInput] = useState('');
  const [searchInput, setSearchInput] = useState('');

  const mainHistoryRef = useRef(new CommandHistory());
  const searchHistoryRef = useRef(new CommandHistory());

  useInput((char, key) => {
    // Ctrl+F - 切换到搜索
    if (key.ctrl && char === 'f') {
      setActiveInput('search');
      return;
    }

    // Escape - 返回主输入
    if (key.escape) {
      setActiveInput('main');
      return;
    }

    // 根据活动输入处理按键
    const isMain = activeInput === 'main';
    const currentInput = isMain ? mainInput : searchInput;
    const setCurrentInput = isMain ? setMainInput : setSearchInput;
    const history = isMain ? mainHistoryRef.current : searchHistoryRef.current;

    if (key.return && currentInput.trim()) {
      history.add(currentInput.trim());
      onCommand(currentInput.trim());
      setCurrentInput('');
      history.reset();
      return;
    }

    if (key.upArrow) {
      const prev = history.previous();
      if (prev !== null) setCurrentInput(prev);
      return;
    }

    if (key.downArrow) {
      const next = history.next();
      if (next !== null) setCurrentInput(next);
      return;
    }

    if (key.backspace || key.delete) {
      setCurrentInput(currentInput.slice(0, -1));
      return;
    }

    if (!key.ctrl && !key.meta && !key.escape && !key.tab) {
      setCurrentInput(currentInput + char);
    }
  });

  return (
    <Box flexDirection="column">
      <Box borderStyle="single" borderColor={activeInput === 'main' ? 'green' : 'gray'}>
        <Text color={activeInput === 'main' ? 'green' : 'gray'}>
          Main: {mainInput}
        </Text>
      </Box>
      <Box borderStyle="single" borderColor={activeInput === 'search' ? 'green' : 'gray'}>
        <Text color={activeInput === 'search' ? 'green' : 'gray'}>
          Search (Ctrl+F): {searchInput}
        </Text>
      </Box>
      <Text dimColor>Active: {activeInput} | Esc: Main | Ctrl+F: Search</Text>
    </Box>
  );
};

// ============================================================================
// 示例 3: 智能命令补全，基于上下文
// ============================================================================

interface ContextualCompletionProps {
  context: 'sql' | 'file' | 'general';
  onSubmit: (cmd: string) => void;
}

const SQL_COMMANDS = [
  'SELECT * FROM ',
  'INSERT INTO ',
  'UPDATE ',
  'DELETE FROM ',
  'CREATE TABLE ',
  'DROP TABLE ',
  'ALTER TABLE ',
  'DESCRIBE ',
];

const FILE_COMMANDS = [
  'ls',
  'cd ',
  'cat ',
  'mkdir ',
  'rm ',
  'cp ',
  'mv ',
];

const GENERAL_COMMANDS = [
  'help',
  'exit',
  'clear',
  'status',
  'version',
];

export const ContextualCompletion: React.FC<ContextualCompletionProps> = ({
  context,
  onSubmit
}) => {
  const [input, setInput] = useState('');
  const completionRef = useRef(new CommandCompletion());

  // 根据上下文更新命令列表
  useEffect(() => {
    let commands: string[];
    switch (context) {
      case 'sql':
        commands = [...SQL_COMMANDS, ...GENERAL_COMMANDS];
        break;
      case 'file':
        commands = [...FILE_COMMANDS, ...GENERAL_COMMANDS];
        break;
      default:
        commands = GENERAL_COMMANDS;
    }
    completionRef.current.setCommands(commands);
  }, [context]);

  useInput((char, key) => {
    if (key.return && input.trim()) {
      onSubmit(input.trim());
      setInput('');
      return;
    }

    if (key.tab) {
      const completion = completionRef.current.complete(input);
      if (completion) {
        setInput(completion);
      }
      return;
    }

    if (key.backspace || key.delete) {
      setInput(input.slice(0, -1));
      completionRef.current.reset();
      return;
    }

    if (!key.ctrl && !key.meta && !key.escape) {
      setInput(input + char);
      completionRef.current.reset();
    }
  });

  const completions = completionRef.current.getCompletions(input);

  return (
    <Box flexDirection="column" borderStyle="round" padding={1}>
      <Text bold color="cyan">Context: {context.toUpperCase()}</Text>
      <Box marginTop={1}>
        <Text color="green" bold>&gt; </Text>
        <Text>{input}</Text>
        <Text inverse> </Text>
      </Box>
      {completions.length > 0 && (
        <Box flexDirection="column" marginTop={1} paddingLeft={2}>
          <Text dimColor>Suggestions:</Text>
          {completions.slice(0, 5).map((cmd, idx) => (
            <Text key={idx} color="yellow">  {cmd}</Text>
          ))}
          {completions.length > 5 && (
            <Text dimColor>  ... and {completions.length - 5} more</Text>
          )}
        </Box>
      )}
    </Box>
  );
};

// ============================================================================
// 示例 4: 完整应用演示
// ============================================================================

export const FullKeybindingsDemo: React.FC = () => {
  const [messages, setMessages] = useState<string[]>([]);
  const [mode, setMode] = useState<'input' | 'help'>('input');

  const handleCommand = (cmd: string) => {
    if (cmd === 'help') {
      setMode('help');
    } else if (cmd === 'clear') {
      setMessages([]);
    } else {
      setMessages([...messages, `> ${cmd}`, `Executed: ${cmd}`]);
    }
  };

  useInput((char, key) => {
    // Ctrl+H - 切换帮助
    if (key.ctrl && char === 'h') {
      setMode(mode === 'help' ? 'input' : 'help');
      return;
    }

    // Ctrl+L - 清空
    if (key.ctrl && char === 'l') {
      setMessages([]);
      return;
    }
  });

  return (
    <Box flexDirection="column" height="100%">
      <Box borderStyle="double" borderColor="cyan" padding={1}>
        <Text bold color="cyan">Keybindings Demo - Press Ctrl+H for help</Text>
      </Box>

      {mode === 'input' ? (
        <Box flexDirection="column" flexGrow={1}>
          <Box flexDirection="column" flexGrow={1} paddingX={1}>
            {messages.slice(-10).map((msg, idx) => (
              <Text key={idx}>{msg}</Text>
            ))}
          </Box>
          <SimpleInputWithHistory
            onSubmit={handleCommand}
            suggestions={[...DEFAULT_COMMANDS, 'help', 'clear']}
          />
        </Box>
      ) : (
        <Box flexDirection="column" padding={2}>
          <Text bold color="cyan" underline>Keyboard Shortcuts</Text>
          <Text> </Text>
          {KEYBINDINGS.map((kb, idx) => (
            <Text key={idx}>
              <Text color="yellow" bold>{kb.key}</Text>
              <Text dimColor> - {kb.description}</Text>
            </Text>
          ))}
          <Text> </Text>
          <Text dimColor italic>Press Ctrl+H to go back</Text>
        </Box>
      )}

      <Box borderStyle="single" borderColor="gray" paddingX={1}>
        <Text dimColor>
          Mode: {mode} | Ctrl+H: Help | Ctrl+L: Clear | Ctrl+C: Clear/Exit
        </Text>
      </Box>
    </Box>
  );
};

// ============================================================================
// 导出所有示例
// ============================================================================

export default {
  SimpleInputWithHistory,
  MultiInputDemo,
  ContextualCompletion,
  FullKeybindingsDemo,
};
