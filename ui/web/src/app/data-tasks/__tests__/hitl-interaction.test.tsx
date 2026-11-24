/** @vitest-environment happy-dom */
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { HitlInteractionHandler } from '../components/chat/HitlInteractionHandler';
import { getHitlPending, setHitlPending } from '../../../lib/hitl-state';

const mocks = vi.hoisted(() => ({
  agent: { isRunning: false, subscribe: vi.fn() },
  runAgent: vi.fn(),
  getNativeSession: vi.fn(),
}));
vi.mock('@copilotkit/react-core/v2', () => ({
  useAgent: () => ({ agent: mocks.agent }),
  useCopilotKit: () => ({ copilotkit: { runAgent: mocks.runAgent } }),
}));
vi.mock('../../../lib/config-api/client', () => ({
  configApi: { getNativeSession: mocks.getNativeSession },
}));

let root: Root;
let host: HTMLDivElement;
const pending = { type: 'human_feedback' as const, interruptId: 'ask', message: 'Which ID?', options: ['user_id'] };
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  mocks.agent.isRunning = false;
  mocks.agent.subscribe.mockReturnValue({ unsubscribe: vi.fn() });
  mocks.runAgent.mockResolvedValue(undefined);
  mocks.getNativeSession.mockResolvedValue({ pendingInteractions: [] });
  setHitlPending('thread', [pending]);
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  setHitlPending('thread', []);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
});
async function mount() {
  await act(async () => root.render(createElement(HitlInteractionHandler, { agentId: 'dataagent', threadId: 'thread' })));
}
async function answer() {
  await act(async () => host.querySelector<HTMLButtonElement>('button[type="button"]')!.click());
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
}

it('displays restored questions and submits selected answers through the existing agent', async () => {
  await mount();
  expect(host.textContent).toContain('Which ID?');
  await answer();
  expect(mocks.runAgent).toHaveBeenCalledWith({
    agent: mocks.agent, forwardedProps: { hitlAnswers: { ask: 'user_id' } },
  });
  expect(mocks.getNativeSession).toHaveBeenCalledWith('thread');
  expect(host.querySelector('form')).toBeNull();
});

it('retains a failed answer for retry when refreshing pending interactions also fails', async () => {
  mocks.runAgent.mockRejectedValue(new Error('Connection lost'));
  mocks.getNativeSession.mockRejectedValue(new Error('Offline'));
  await mount();
  await answer();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('Connection lost');
  expect(getHitlPending('thread')).toEqual([pending]);
  expect(host.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(false);
});

it('replaces pending questions from SSE without mixing another thread', async () => {
  setHitlPending('other', [{ ...pending, interruptId: 'other' }]);
  await mount();
  const subscriber = mocks.agent.subscribe.mock.calls[0]![0];
  await act(async () => subscriber.onCustomEvent({ event: {
    name: 'human_feedback', value: [{ ...pending, interruptId: 'next', validationError: 'Please clarify' }],
  } }));
  expect(getHitlPending('thread')[0]?.interruptId).toBe('next');
  expect(host.textContent).toContain('Please clarify');
  expect(getHitlPending('other')[0]?.interruptId).toBe('other');
  setHitlPending('other', []);
});
