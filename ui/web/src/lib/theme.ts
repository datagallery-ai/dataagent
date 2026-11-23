export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "datagallery-theme";

/**
 * Runs in <head> before first paint so the page never flashes the wrong theme.
 * Keep it dependency-free and in sync with applyThemePreference below.
 */
export const THEME_INIT_SCRIPT = `(function(){try{var p=localStorage.getItem("${THEME_STORAGE_KEY}");if(p!=="light"&&p!=="dark")p="system";var d=p==="dark"||(p==="system"&&window.matchMedia("(prefers-color-scheme: dark)").matches);var r=document.documentElement;r.setAttribute("data-theme",d?"dark":"light");r.setAttribute("data-theme-pref",p);r.classList.toggle("dark",d);}catch(e){}})();`;

export function readThemePreference(): ThemePreference {
  if (typeof document === "undefined") return "system";
  const pref = document.documentElement.getAttribute("data-theme-pref");
  return pref === "light" || pref === "dark" ? pref : "system";
}

export function resolveTheme(pref: ThemePreference): ResolvedTheme {
  if (pref !== "system") return pref;
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export type ThemeSnapshot = {
  pref: ThemePreference;
  resolved: ResolvedTheme;
};

const SERVER_THEME_SNAPSHOT: ThemeSnapshot = { pref: "system", resolved: "light" };
let themeSnapshot: ThemeSnapshot = SERVER_THEME_SNAPSHOT;
let themeListeners = new Set<() => void>();
let themeDomReady = false;

function publishTheme(next: ThemeSnapshot) {
  if (themeSnapshot.pref === next.pref && themeSnapshot.resolved === next.resolved) return;
  themeSnapshot = next;
  for (const listener of themeListeners) listener();
}

function readDomTheme(): ThemeSnapshot {
  const pref = readThemePreference();
  const attr = document.documentElement.getAttribute("data-theme");
  const resolved: ResolvedTheme = attr === "dark" || attr === "light" ? attr : resolveTheme(pref);
  return { pref, resolved };
}

function watchSystemTheme() {
  if (typeof window === "undefined") return;
  if (window.matchMedia) {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    query.addEventListener("change", () => {
      if (themeSnapshot.pref !== "system") return;
      applyThemePreference("system");
    });
  }
  window.addEventListener("storage", (event) => {
    if (event.key !== THEME_STORAGE_KEY) return;
    const pref: ThemePreference = event.newValue === "light" || event.newValue === "dark" ? event.newValue : "system";
    applyThemePreference(pref);
  });
}

function ensureClientTheme() {
  if (themeDomReady || typeof document === "undefined") return;
  themeDomReady = true;
  themeSnapshot = readDomTheme();
  watchSystemTheme();
}

export function subscribeTheme(listener: () => void): () => void {
  ensureClientTheme();
  themeListeners.add(listener);
  return () => {
    themeListeners.delete(listener);
  };
}

export function getThemeSnapshot(): ThemeSnapshot {
  ensureClientTheme();
  return themeSnapshot;
}

export function getServerThemeSnapshot(): ThemeSnapshot {
  return SERVER_THEME_SNAPSHOT;
}

export function applyThemePreference(pref: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(pref);
  if (typeof document !== "undefined") {
    const root = document.documentElement;
    root.setAttribute("data-theme", resolved);
    root.setAttribute("data-theme-pref", pref);
    // CopilotKit's own stylesheet keys its dark variants off a `.dark` ancestor.
    root.classList.toggle("dark", resolved === "dark");
    try {
      if (pref === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
      else window.localStorage.setItem(THEME_STORAGE_KEY, pref);
    } catch {
      // Storage can be unavailable (private mode); the attribute still applies for this page.
    }
  }
  publishTheme({ pref, resolved });
  return resolved;
}
