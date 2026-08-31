"use client";

import React, { type ComponentPropsWithoutRef, type ReactNode } from "react";
import { CopilotChatAssistantMessage } from "@copilotkit/react-core/v2";

/**
 * Streamdown (via CopilotKit MarkdownRenderer) ships `cpk:*` utilities that only
 * apply under `[data-copilotkit]`. Without that scope, long paragraphs do not wrap
 * and get clipped by the Outputs pane's overflow-x-hidden.
 *
 * Shared by chat final answers and Outputs MD previews so GFM (tables, lists,
 * code, links) renders consistently with design-system tokens.
 */
const markdownBaseClass = [
  "min-w-0 w-full max-w-full break-words text-foreground [overflow-wrap:anywhere]",
  "[&_*]:max-w-full",
  "[&_p]:my-2 [&_p]:whitespace-normal [&_p]:break-words",
  "[&_li]:my-0.5 [&_li]:break-words",
  "[&_h1]:mb-2 [&_h1]:mt-4 [&_h1]:break-words [&_h1]:text-base [&_h1]:font-semibold [&_h1]:leading-7 first:[&_h1]:mt-0",
  "[&_h2]:mb-2 [&_h2]:mt-3 [&_h2]:break-words [&_h2]:text-sm [&_h2]:font-semibold [&_h2]:leading-6 first:[&_h2]:mt-0",
  "[&_h3]:mb-1 [&_h3]:mt-3 [&_h3]:break-words [&_h3]:text-sm [&_h3]:font-semibold first:[&_h3]:mt-0",
  "[&_h4]:mb-1 [&_h4]:mt-2 [&_h4]:text-sm [&_h4]:font-medium",
  "[&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5",
  "[&_blockquote]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted",
  "[&_hr]:my-4 [&_hr]:border-border",
  "[&_img]:my-2 [&_img]:h-auto [&_img]:max-w-full [&_img]:rounded-md",
  "[&_code]:rounded [&_code]:bg-surface-subtle [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.9em] [&_code]:break-words",
  "[&_pre]:my-2 [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:bg-surface-subtle [&_pre]:p-3",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_pre_code]:text-[12px] [&_pre_code]:leading-5",
  "[&_table]:my-2 [&_table]:w-full [&_table]:min-w-full [&_table]:border-collapse [&_table]:text-left [&_table]:text-xs",
  "[&_thead]:bg-surface-subtle",
  "[&_th]:border [&_th]:border-border [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:font-semibold [&_th]:text-foreground",
  "[&_td]:border [&_td]:border-border [&_td]:px-2.5 [&_td]:py-1.5 [&_td]:align-top [&_td]:text-muted",
  "[&_a]:font-medium [&_a]:text-primary [&_a]:underline [&_a]:decoration-primary/30 [&_a]:underline-offset-2 hover:[&_a]:decoration-primary",
].join(" ");

const densityClass = {
  chat: "text-sm leading-7",
  artifact: "text-sm leading-6",
  compact: "text-xs leading-5",
} as const;

/** Card chrome + height cap — used for inline/embedded previews, not the peer page. */
const markdownChromeClass = "max-h-80 overflow-auto rounded-lg border border-border bg-surface p-3";

const streamdownProseClass = "cpk:prose cpk:max-w-full cpk:break-words cpk:dark:prose-invert";

export type RichMarkdownDensity = keyof typeof densityClass;

export function isMarkdownFilePath(path: string): boolean {
  return /\.(?:md|markdown|mdx)$/iu.test(path);
}

function isSafeHttpUrl(href: string | undefined): boolean {
  if (!href) return false;
  try {
    const url = new URL(href, "https://local.invalid");
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function SecureMarkdownLink({
  href,
  children,
  ...props
}: ComponentPropsWithoutRef<"a"> & { node?: unknown }) {
  const { node: _node, ...rest } = props;
  const external = isSafeHttpUrl(href) && !href?.startsWith("#");
  // Block javascript: / data: etc. — render as inert text instead of a navigable link.
  if (href && !href.startsWith("#") && !isSafeHttpUrl(href) && !href.startsWith("/")) {
    return <span className="break-all">{children}</span>;
  }
  return (
    <a
      {...rest}
      href={href}
      {...(external ? { target: "_blank", rel: "noopener noreferrer" } : {})}
    >
      {children}
    </a>
  );
}

type MarkdownRendererProps = {
  content: string;
  className?: string;
  components?: {
    a?: typeof SecureMarkdownLink;
  };
};

const MarkdownRenderer = CopilotChatAssistantMessage.MarkdownRenderer as (
  props: MarkdownRendererProps,
) => ReactNode;

/**
 * Unified markdown surface for assistant final answers and artifact MD previews.
 * Uses Streamdown (GFM, code highlighting, sanitization) via CopilotKit.
 */
export function RichMarkdown({
  content,
  density = "artifact",
  className,
}: {
  content: string;
  density?: RichMarkdownDensity;
  className?: string;
}) {
  return (
    <div
      data-copilotkit
      className={[markdownBaseClass, densityClass[density], className].filter(Boolean).join(" ")}
    >
      <div className="min-w-0 max-w-full overflow-x-auto">
        <MarkdownRenderer
          content={content}
          className={streamdownProseClass}
          components={{ a: SecureMarkdownLink }}
        />
      </div>
    </div>
  );
}

/**
 * `bare` renders the markdown flush (no bordered box, no height cap) so a
 * dedicated peer page can use the full available height.
 */
export function ArtifactMarkdownPreview({
  content,
  bare = false,
}: {
  content: string;
  bare?: boolean;
}) {
  if (bare) {
    return <RichMarkdown content={content} density="artifact" />;
  }
  return (
    <div className={markdownChromeClass}>
      <RichMarkdown content={content} density="artifact" />
    </div>
  );
}
