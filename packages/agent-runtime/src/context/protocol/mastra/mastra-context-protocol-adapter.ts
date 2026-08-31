import type { MastraDBMessage } from "@mastra/core/agent";

import type { ContextPromptView } from "../../projection/context-prompt-view.js";
import type { ContextProtocolAdapter } from "../context-protocol-adapter.js";
import { toMastraDBMessages } from "./mastra-context-prompt-message-adapter.js";

export type MastraContextProtocolOutput = {
  messages: MastraDBMessage[];
  systemMessages: unknown[];
};

export class MastraContextProtocolAdapter
  implements ContextProtocolAdapter<ContextPromptView, MastraContextProtocolOutput> {
  readonly protocol = "mastra";

  toProtocol(view: ContextPromptView): MastraContextProtocolOutput {
    return {
      messages: toMastraDBMessages(view.messages),
      systemMessages: view.systemMessages
    };
  }
}
