import { EventSchemas, type BaseEvent } from '@ag-ui/core';
import { z } from 'zod';

// Python AG-UI already emits these native events; @ag-ui/core 0.0.57 does not
// declare them yet. Keep their wire format intact at this compatibility boundary.
const child = z.object({
  subagentRunId: z.string().min(1), timestamp: z.number().optional(), rawEvent: z.unknown().optional(),
}).passthrough();
const SubagentEventSchema = z.discriminatedUnion('type', [
  child.extend({
    type: z.literal('SUBAGENT_STARTED'), name: z.string().min(1),
    description: z.string().nullish(), parentSubagentRunId: z.string().nullish(),
    parentToolCallId: z.string().nullish(), parentMessageId: z.string().nullish(),
  }),
  child.extend({
    type: z.literal('SUBAGENT_FINISHED'), result: z.unknown().optional(),
    outcome: z.union([
      z.object({ type: z.literal('success') }),
      z.object({ type: z.literal('suspended'), interruptIds: z.array(z.string()).nullish() }),
    ]).nullish(),
  }),
  child.extend({ type: z.literal('SUBAGENT_ERROR'), message: z.string(), code: z.string().nullish() }),
]);

export type AgentEvent = BaseEvent | z.infer<typeof SubagentEventSchema>;

export function parseAgentEvent(input: unknown): AgentEvent {
  const type = input && typeof input === 'object' && 'type' in input ? input.type : undefined;
  return typeof type === 'string' && type.startsWith('SUBAGENT_')
    ? SubagentEventSchema.parse(input) : EventSchemas.parse(input);
}
