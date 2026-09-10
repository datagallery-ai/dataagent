const INTERNAL_CONTEXT_TAGS = [
  'working_memory_data',
  'long_term_memory',
];

export type AssistantTextFlush =
  | { type: 'text'; content: string; isStreaming: boolean }
  | { type: 'finalize' };

const escapeRegExp = (value: string): string => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export const stripInternalContextBlocks = (text: string): string => {
  let result = text;

  for (const tag of INTERNAL_CONTEXT_TAGS) {
    const escapedTag = escapeRegExp(tag);
    result = result.replace(
      new RegExp(`<${escapedTag}>[\\s\\S]*?<\\/${escapedTag}>\\s*`, 'g'),
      '',
    );
    result = result.replace(
      new RegExp(`<${escapedTag}>[\\s\\S]*$`, 'g'),
      '',
    );

    const openTag = `<${tag}>`;
    for (let length = openTag.length - 1; length > 0; length -= 1) {
      if (result.endsWith(openTag.slice(0, length))) {
        result = result.slice(0, -length);
        break;
      }
    }
  }

  return result;
};

export class AssistantTextStreamBuffer {
  private rawText = '';
  private visibleText = '';
  private flushedVisibleText = '';
  private currentSegmentBase = '';
  private currentSegmentStart = 0;

  append(delta: string): boolean {
    // AG-UI TEXT_MESSAGE_CONTENT is a delta, including repeated tokens.
    this.rawText += delta;
    const nextVisibleText = stripInternalContextBlocks(this.rawText);
    if (nextVisibleText === this.visibleText) {
      return false;
    }
    this.visibleText = nextVisibleText;
    return true;
  }

  flush(isStreaming = true): AssistantTextFlush | null {
    if (this.visibleText !== this.flushedVisibleText) {
      const content = this.currentVisibleTextSegment();
      this.flushedVisibleText = this.visibleText;
      return { type: 'text', content, isStreaming };
    }

    if (!isStreaming) {
      return { type: 'finalize' };
    }

    return null;
  }

  markSegmentBoundary(): void {
    this.currentSegmentBase = this.visibleText;
    this.currentSegmentStart = this.visibleText.length;
  }

  private currentVisibleTextSegment(): string {
    if (this.visibleText.startsWith(this.currentSegmentBase)) {
      return this.visibleText.slice(this.currentSegmentBase.length);
    }

    return this.visibleText.slice(Math.min(this.currentSegmentStart, this.visibleText.length));
  }
}
