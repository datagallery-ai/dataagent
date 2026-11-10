/** @vitest-environment happy-dom */
import { describe, expect, it } from "vitest";
import { applyThemePreference, getThemeSnapshot, subscribeTheme } from "./theme";

describe("theme snapshot", () => {
  it("notifies every subscriber from one preference change", () => {
    document.documentElement.setAttribute("data-theme", "light");
    document.documentElement.setAttribute("data-theme-pref", "light");
    const seen: string[] = [];
    const stop = subscribeTheme(() => {
      seen.push(getThemeSnapshot().resolved);
    });

    expect(getThemeSnapshot()).toEqual({ pref: "light", resolved: "light" });
    applyThemePreference("dark");

    expect(getThemeSnapshot()).toEqual({ pref: "dark", resolved: "dark" });
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(seen).toEqual(["dark"]);

    applyThemePreference("light");
    expect(getThemeSnapshot().resolved).toBe("light");
    stop();
  });
});
