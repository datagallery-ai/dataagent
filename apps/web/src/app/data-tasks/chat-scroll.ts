function isScrollableOverflow(overflowY: string): boolean {
  return overflowY === "auto" || overflowY === "scroll";
}

function readOverflowY(element: HTMLElement): string {
  const inlineOverflowY = element.style?.overflowY;
  if (inlineOverflowY) {
    return inlineOverflowY;
  }
  if (typeof getComputedStyle === "function") {
    return getComputedStyle(element).overflowY;
  }
  return "";
}

function isRendered(element: HTMLElement): boolean {
  let current: HTMLElement | null = element;
  while (current) {
    if (current.style?.display === "none") {
      return false;
    }
    if (typeof getComputedStyle === "function") {
      const style = getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden") {
        return false;
      }
    }
    current = current.parentElement;
  }
  return true;
}

function findScrollableAncestor(element: HTMLElement): HTMLElement | null {
  let current = element.parentElement;
  let fallback: HTMLElement | null = null;
  while (current) {
    if (isScrollableOverflow(readOverflowY(current))) {
      fallback ??= current;
      if (current.scrollHeight > current.clientHeight) {
        return current;
      }
    }
    current = current.parentElement;
  }
  return fallback;
}

export function findCopilotChatScrollContainer(
  root: ParentNode = document,
): HTMLElement | null {
  const contents = root.querySelectorAll('[data-testid="copilot-scroll-content"]');
  for (const content of contents) {
    if (typeof content !== "object" || !("parentElement" in content)) {
      continue;
    }
    const element = content as HTMLElement;
    if (!isRendered(element)) {
      continue;
    }
    const scrollContainer = findScrollableAncestor(element);
    if (scrollContainer) {
      return scrollContainer;
    }
  }

  return null;
}

function applyScrollTop(
  container: HTMLElement,
  top: number,
  behavior: ScrollBehavior = "auto",
) {
  const previousScrollBehavior = container.style.scrollBehavior;
  if (behavior === "auto") {
    container.style.scrollBehavior = "auto";
  }
  const nextTop = Math.max(0, top);
  container.scrollTop = nextTop;
  container.scrollTo({
    top: nextTop,
    behavior,
  });
  if (behavior === "auto") {
    container.style.scrollBehavior = previousScrollBehavior;
  }
}

export function scrollCopilotChatToBottom(
  root: ParentNode = document,
  behavior: ScrollBehavior = "auto",
): boolean {
  const container = findCopilotChatScrollContainer(root);
  if (!container) {
    return false;
  }

  applyScrollTop(container, container.scrollHeight, behavior);
  return true;
}

export function restoreCopilotChatScrollTop(
  scrollTop: number,
  root: ParentNode = document,
  behavior: ScrollBehavior = "auto",
): boolean {
  const container = findCopilotChatScrollContainer(root);
  if (!container) {
    return false;
  }

  const maxScrollTop = Math.max(0, container.scrollHeight - container.clientHeight);
  applyScrollTop(container, Math.min(Math.max(0, scrollTop), maxScrollTop), behavior);
  return true;
}

export function restoreCopilotChatScrollTopWithRetries(input: {
  scrollTop: number;
  root?: ParentNode;
  attempts?: number;
  intervalMs?: number;
  schedule?: (callback: () => void) => void;
  delay?: (callback: () => void, intervalMs: number) => number;
}): () => void {
  const {
    scrollTop,
    root = document,
    // Branch restores can keep laying out for a few hundred ms; keep re-applying.
    attempts = 24,
    intervalMs = 50,
    schedule = (callback: () => void) => requestAnimationFrame(callback),
    delay = (callback: () => void, waitMs: number) => window.setTimeout(callback, waitMs),
  } = input;

  let cancelled = false;
  let attempt = 0;

  const tick = () => {
    if (cancelled) {
      return;
    }
    restoreCopilotChatScrollTop(scrollTop, root, "auto");
    attempt += 1;
    if (attempt < attempts) {
      delay(tick, intervalMs);
    }
  };

  schedule(tick);

  return () => {
    cancelled = true;
  };
}

export function scrollCopilotChatToBottomWithRetries(input?: {
  root?: ParentNode;
  attempts?: number;
  intervalMs?: number;
  schedule?: (callback: () => void) => void;
  delay?: (callback: () => void, intervalMs: number) => number;
}): () => void {
  const {
    root = document,
    attempts = 8,
    intervalMs = 50,
    schedule = (callback: () => void) => requestAnimationFrame(callback),
    delay = (callback: () => void, waitMs: number) => window.setTimeout(callback, waitMs),
  } = input ?? {};

  let cancelled = false;
  let attempt = 0;

  const tick = () => {
    if (cancelled) {
      return;
    }
    scrollCopilotChatToBottom(root, "auto");
    attempt += 1;
    if (attempt < attempts) {
      delay(tick, intervalMs);
    }
  };

  schedule(tick);

  return () => {
    cancelled = true;
  };
}
