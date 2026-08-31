import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

vi.mock("@copilotkit/react-core/v2", () => ({
  CopilotChatAssistantMessage: {
    MarkdownRenderer: ({
      content,
      components,
    }: {
      content: string;
      components?: { a?: (props: { href?: string; children?: unknown }) => unknown };
    }) => {
      const Link = components?.a;
      if (Link && content.includes("http")) {
        return createElement(
          "div",
          null,
          createElement(Link, { href: "https://example.com" }, "safe"),
          createElement(Link, { href: "javascript:alert(1)" }, "blocked"),
        );
      }
      return createElement("div", { "data-md": true }, content);
    },
  },
}));

import {
  ArtifactMarkdownPreview,
  isMarkdownFilePath,
  RichMarkdown,
} from "../components/task-console/ArtifactMarkdownPreview";

describe("isMarkdownFilePath", () => {
  it("recognizes common markdown extensions", () => {
    expect(isMarkdownFilePath("notes.md")).toBe(true);
    expect(isMarkdownFilePath("README.markdown")).toBe(true);
    expect(isMarkdownFilePath("doc.MDX")).toBe(true);
    expect(isMarkdownFilePath("data.csv")).toBe(false);
  });
});

describe("RichMarkdown", () => {
  it("scopes Streamdown under data-copilotkit and renders content", () => {
    const html = renderToStaticMarkup(
      createElement(RichMarkdown, { content: "# Hello\n\n- item", density: "chat" }),
    );
    expect(html).toContain("data-copilotkit");
    expect(html).toContain("# Hello");
  });

  it("sanitizes unsafe link protocols", () => {
    const html = renderToStaticMarkup(
      createElement(RichMarkdown, { content: "see http link", density: "artifact" }),
    );
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).not.toContain("javascript:");
    expect(html).toContain(">blocked</span>");
  });
});

describe("ArtifactMarkdownPreview", () => {
  it("applies chrome by default and skips it when bare", () => {
    const chrome = renderToStaticMarkup(
      createElement(ArtifactMarkdownPreview, { content: "body" }),
    );
    const bare = renderToStaticMarkup(
      createElement(ArtifactMarkdownPreview, { content: "body", bare: true }),
    );
    expect(chrome).toContain("max-h-80");
    expect(bare).not.toContain("max-h-80");
  });
});
