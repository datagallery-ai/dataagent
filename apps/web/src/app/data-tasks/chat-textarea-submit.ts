import type { KeyboardEvent as ReactKeyboardEvent } from "react";

type EnterKeyEvent = Pick<ReactKeyboardEvent, "key" | "shiftKey" | "keyCode"> & {
  nativeEvent: { isComposing?: boolean };
};

/** True when Enter should submit (not newline). Respects IME composition. */
export function shouldSubmitChatTextareaOnEnter(event: EnterKeyEvent): boolean {
  if (event.nativeEvent.isComposing || event.keyCode === 229) {
    return false;
  }
  return event.key === "Enter" && !event.shiftKey;
}

export function createChatTextareaKeyDownHandler(onSubmit: () => void) {
  return (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (!shouldSubmitChatTextareaOnEnter(event)) {
      return;
    }
    event.preventDefault();
    onSubmit();
  };
}

/** Capture-phase handler so parent can submit before nested handlers run. */
export function createChatTextareaKeyDownCaptureHandler(onSubmit: () => void) {
  return (event: ReactKeyboardEvent<HTMLElement>) => {
    if (!(event.target instanceof HTMLTextAreaElement)) {
      return;
    }
    if (!shouldSubmitChatTextareaOnEnter(event)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    onSubmit();
  };
}
