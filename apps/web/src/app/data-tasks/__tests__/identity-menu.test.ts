import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = () =>
  readFileSync(join(process.cwd(), "src/app/data-tasks/data-task-identity.tsx"), "utf8");

describe("data task identity menu", () => {
  it("keeps the account menu focused on the current user and core actions", () => {
    const file = source();

    for (const label of [
      't("userBar.settings")',
      't("userBar.signOut")',
    ]) {
      expect(file).toContain(label);
    }

    expect(file).toContain("LanguageToggle");
    expect(file).toContain("AccountMoreIcon");
    expect(file).toContain("onOpenSettings");
  });

  it("wires compact avatar clicks to onOpenSettings", () => {
    const file = source();

    expect(file).toContain("if (compact)");
    expect(file).toContain("onClick={() => onOpenSettings?.()}");
  });

  it("expands the collapsed rail before opening settings from the compact avatar", () => {
    const page = readFileSync(
      join(process.cwd(), "src/app/data-tasks/data-tasks-app.tsx"),
      "utf8",
    );

    expect(page).toContain("DataTaskUserBar");
    expect(page).toContain("compact");
    expect(page).toMatch(
      /onOpenSettings=\{\(\) => \{\s*onToggleCollapse\(\);\s*onOpenConfigPanel\("llm"\);\s*\}\}/,
    );
  });

  it("does not expose development identity paths", () => {
    const file = source();

    for (const label of [
      "Personal account",
      "Profile",
      "Usage",
      "Favorites",
      "API service",
      "Download desktop",
      "Download mobile",
      "Switch account",
      "Add account",
      "Local dev users authenticate with dev tokens",
      "Continue as Dev User",
      "DEV_SIGNED_OUT_STORAGE_KEY",
      "DevIdentityProvider",
      "getDevIdentities",
    ]) {
      expect(file).not.toContain(label);
    }
  });

  it("uses a divider-only trigger and a flush animated popover", () => {
    const file = source();

    expect(file).toContain("bottom-full");
    expect(file).toContain("account-menu-popover-in");
    expect(file).toContain("hover:bg-surface/60");
    expect(file).not.toContain("bottom-[calc(100%+8px)]");
    expect(file).not.toContain("rounded-lg border border-border bg-surface px-2 py-2");
  });

  it("exposes production password auth screens and account actions", () => {
    const authFlow = readFileSync(
      join(process.cwd(), "src/components/auth/auth-flow.tsx"),
      "utf8",
    );

    for (const label of [
      "Sign in",
      "Create account",
      "Forgot password",
      "Verify email",
    ]) {
      expect(authFlow).toContain(label);
    }

    const file = source();
    expect(file).toContain("PasswordAuthShell");
    expect(file).toContain('authMode: "password"');
  });
});
