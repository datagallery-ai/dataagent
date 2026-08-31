import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const source = (path: string) => readFileSync(join(process.cwd(), path), "utf8");

describe("quick start guide integration", () => {
  it("mounts the quick start guide from the data tasks page", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");

    expect(page).toContain("QuickStartGuide");
    expect(page).toContain("quickStartGuide=");
    expect(page).toContain("onUseExampleQuery=");
    expect(page).toContain("draftPromptRequest");
  });

  it("places the quick-start launcher in the user bar as a round question button", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");
    const guide = source(
      "src/app/data-tasks/components/guide/QuickStartGuide.tsx",
    );
    const identity = source("src/app/data-tasks/data-task-identity.tsx");

    expect(page).toContain("quickStartGuide={quickStartGuide}");
    expect(identity).toContain("quickStartGuide?: ReactNode");
    expect(guide).toContain('aria-label={t("guide.open")}');
    expect(guide).toContain("rounded-full");
    expect(guide).not.toContain(">Guide<");
  });

  it("provides stable guide anchors for the quick-start path", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");
    const chatInput = source(
      "src/app/data-tasks/components/chat/DataTaskChatInput.tsx",
    );
    const taskConsole = source(
      "src/app/data-tasks/components/task-console/TaskConsole.tsx",
    );

    expect(page).toContain('data-guide-id="workspace-layout"');
    expect(page).toContain('data-guide-id="workspace-resources"');
    expect(page).toContain("data-guide-id={");
    expect(page).toContain('"datasource-config"');
    expect(chatInput).toContain(
      'data-guide-id="model-picker"',
    );
    expect(chatInput).toContain('ref={rootRef}');
    expect(chatInput).toContain('data-guide-id="chat-input"');
    expect(taskConsole).toContain('data-guide-id="run-console"');
    expect(taskConsole).toContain('data-guide-id="run-output"');
  });

  it("fills the chat draft through the chat input binding instead of clipboard-only copy", () => {
    const guide = source(
      "src/app/data-tasks/components/guide/QuickStartGuide.tsx",
    );
    const bindings = source(
      "src/app/data-tasks/components/chat/DataTaskChatInputBindingsContext.tsx",
    );
    const chatInput = source(
      "src/app/data-tasks/components/chat/DataTaskChatInput.tsx",
    );

    expect(guide).toContain("onUseExampleQuery");
    expect(guide).not.toContain("navigator.clipboard.writeText");
    expect(bindings).toContain("draftPromptRequest");
    expect(bindings).toContain("onDraftPromptConsumed");
    expect(chatInput).toContain("dispatchEvent(new Event(\"input\"");
    expect(chatInput).toContain("onDraftPromptConsumed");
  });

  it("renders an explicit empty chat welcome overlay outside CopilotChat internals", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");
    const welcome = source("src/app/data-tasks/components/chat/DataTaskWelcome.tsx");

    expect(page).toContain("ChatWelcomeOverlay");
    expect(page).toContain("hasVisibleMessages");
    expect(page).toContain("<ChatWelcomeOverlay");
    expect(welcome).toContain("onUsePrompt?:");
    expect(welcome).toContain('t("welcome.useThisPrompt")');
  });

  it("clears draft prompt requests when switching or creating sessions", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");

    expect(page).toContain("clearDraftPromptRequest");
    expect(page).toContain("onCreateSession={createSession}");
    expect(page).toContain("clearDraftPromptRequest();");
  });

  it("shows guide progress and blocks the send step until a task starts", () => {
    const page = source("src/app/data-tasks/data-tasks-app.tsx");
    const guide = source(
      "src/app/data-tasks/components/guide/QuickStartGuide.tsx",
    );

    expect(page).toContain("hasSubmittedTask={");
    expect(guide).toContain('t("guide.stepOf"');
    expect(guide).toContain("disabled={step.blocked}");
    expect(guide).toContain('aria-disabled={step.blocked ? "true" : undefined}');
  });
});
