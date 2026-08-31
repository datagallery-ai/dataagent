export interface TuiThemeTokens {
  background: {
    canvas: string;
    surface: string;
    overlay: string;
  };
  text: {
    primary: string;
    emphasis: string;
    secondary: string;
    muted: string;
    disabled: string;
  };
  border: {
    default: string;
    focused: string;
    overlay: string;
  };
  structure: {
    accent: string;
    focus: string;
  };
  interaction: {
    accent: string;
  };
  status: {
    success: string;
    warning: string;
    error: string;
  };
  selection: {
    background: string;
    selectedBackground: string;
    border: string;
    heading: string;
    selectedTitle: string;
    title: string;
    accent: string;
    selectedDescription: string;
    description: string;
    disabled: string;
  };
}

export interface TuiThemePreset {
  name: string;
  aliases?: readonly string[] | undefined;
  tokens: TuiThemeTokens;
}
