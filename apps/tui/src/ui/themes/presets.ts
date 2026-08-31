import type { TuiThemePreset } from './types.js';

/**
 * DataFoundry 默认主题：蓝色负责结构，雾青负责操作和选择，冷灰负责说明。
 */
export const mistDarkTheme: TuiThemePreset = {
  name: 'mist-dark',
  aliases: ['default', 'mist'],
  tokens: {
    background: {
      canvas: '#0B0F14',
      surface: '#121820',
      overlay: '#111719',
    },
    text: {
      primary: '#E6EDF3',
      emphasis: '#B7C0C8',
      secondary: '#7D8590',
      muted: '#5F6975',
      disabled: '#586368',
    },
    border: {
      default: '#27313C',
      focused: '#496783',
      overlay: '#29383E',
    },
    structure: {
      accent: '#6CA8E8',
      focus: '#496783',
    },
    interaction: {
      accent: '#79A5A9',
    },
    status: {
      success: '#88C980',
      warning: '#D8B76A',
      error: '#F87171',
    },
    selection: {
      background: '#111719',
      selectedBackground: '#1B272C',
      border: '#29383E',
      heading: '#D5DEDE',
      selectedTitle: '#D5DEDE',
      title: '#A9B8B9',
      accent: '#79A5A9',
      selectedDescription: '#98A6A8',
      description: '#707C81',
      disabled: '#586368',
    },
  },
};

/**
 * 保留旧版蓝色交互风格，方便回退和对比，也作为新增主题的最小示例。
 */
export const legacyDarkTheme: TuiThemePreset = {
  name: 'legacy-dark',
  aliases: ['legacy'],
  tokens: {
    background: {
      canvas: '#0B0F14',
      surface: '#121820',
      overlay: '#121820',
    },
    text: {
      primary: '#E6EDF3',
      emphasis: '#B7C0C8',
      secondary: '#7D8590',
      muted: '#5F6975',
      disabled: '#5F6975',
    },
    border: {
      default: '#27313C',
      focused: '#496783',
      overlay: '#27313C',
    },
    structure: {
      accent: '#6CA8E8',
      focus: '#496783',
    },
    interaction: {
      accent: '#6CA8E8',
    },
    status: {
      success: '#88C980',
      warning: '#D8B76A',
      error: '#F87171',
    },
    selection: {
      background: '#121820',
      selectedBackground: '#18283A',
      border: '#27313C',
      heading: '#E6EDF3',
      selectedTitle: '#E6EDF3',
      title: '#B7C0C8',
      accent: '#6CA8E8',
      selectedDescription: '#B7C0C8',
      description: '#7D8590',
      disabled: '#5F6975',
    },
  },
};

export const builtInThemes = [
  mistDarkTheme,
  legacyDarkTheme,
] as const satisfies readonly TuiThemePreset[];
