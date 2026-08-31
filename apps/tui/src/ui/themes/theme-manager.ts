import { builtInThemes, mistDarkTheme } from './presets.js';
import type { TuiThemePreset, TuiThemeTokens } from './types.js';

const normalizeThemeName = (name: string): string => name.trim().toLowerCase();

export class TuiThemeManager {
  private activeTheme: TuiThemePreset = mistDarkTheme;

  getActiveTheme(): TuiThemePreset {
    return this.activeTheme;
  }

  getTokens(): TuiThemeTokens {
    return this.activeTheme.tokens;
  }

  getAvailableThemes(): readonly TuiThemePreset[] {
    return builtInThemes;
  }

  setActiveTheme(name: string | undefined): boolean {
    if (!name) {
      this.activeTheme = mistDarkTheme;
      return true;
    }

    const normalizedName = normalizeThemeName(name);
    const theme = builtInThemes.find((candidate) => {
      return normalizeThemeName(candidate.name) === normalizedName
        || candidate.aliases?.some((alias) => normalizeThemeName(alias) === normalizedName);
    });
    if (!theme) return false;

    this.activeTheme = theme;
    return true;
  }
}

export const themeManager = new TuiThemeManager();
