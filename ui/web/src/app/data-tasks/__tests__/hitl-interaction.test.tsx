/** @vitest-environment happy-dom */
import React, { act, createElement, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { AgentSubscriber, RunAgentInput } from '@ag-ui/client';
import { HitlInteractionHandler, HitlQuestion, useHitlInteraction } from '../components/chat/HitlInteractionHandler';
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
vi.mock('../../../i18n/locale-context', async () => {
  const { createTranslator } = await import('../../../i18n/translate');
  return { useT: () => createTranslator('zh-CN') };
});

// Exercise both input methods against one controller and one shared textarea.
function Composer() {
  const hitl = useHitlInteraction('dataagent', 'thread');
  const [answer, setAnswer] = useState('');
  async function submit(value: string) {
    if (await hitl.submitAnswer(value)) setAnswer('');
  }
  return <div data-testid="composer">
    {hitl.current && <HitlQuestion interaction={hitl.current} pendingCount={hitl.pending.length}
      disabled={hitl.disabled} error={hitl.error} onAnswer={value => { void submit(value); }} />}
    <form onSubmit={e => { e.preventDefault(); void submit(answer); }}>
      <textarea aria-label="主输入框" value={answer} disabled={hitl.disabled}
        onChange={e => setAnswer(e.target.value)} />
      <button type="submit" disabled={hitl.disabled || !answer.trim()}>发送</button>
    </form>
  </div>;
}

let root: Root;
let host: HTMLDivElement;
const subscribers = new Set<AgentSubscriber>();
function answerRunParams(forwardedProps: Record<string, unknown> = { hitlAnswers: { ask: 'user_id' } }) {
  return {
    input: { threadId: 'thread', runId: 'answer-run', messages: [], tools: [], context: [], state: {}, forwardedProps } satisfies RunAgentInput,
    agent: mocks.agent as unknown as Parameters<NonNullable<AgentSubscriber['onRunStartedEvent']>>[0]['agent'],
    messages: [], state: {},
  };
}
async function acceptAnswer(forwardedProps?: Record<string, unknown>) {
  for (const subscriber of subscribers) {
    await subscriber.onRunStartedEvent?.({ ...answerRunParams(forwardedProps),
      event: { type: 'RUN_STARTED', threadId: 'thread', runId: 'answer-run' } });
  }
}
const pending = { type: 'human_feedback' as const, interruptId: 'ask', message: 'Which ID?', options: ['user_id'] };
beforeEach(() => {
  vi.stubGlobal('React', React);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  mocks.agent.isRunning = false;
  subscribers.clear();
  mocks.agent.subscribe.mockImplementation((subscriber: AgentSubscriber) => {
    subscribers.add(subscriber);
    return { unsubscribe: () => { subscribers.delete(subscriber); } };
  });
  mocks.runAgent.mockImplementation(async ({ forwardedProps }) => { await acceptAnswer(forwardedProps); });
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
  setHitlPending('other', []);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = false;
  vi.unstubAllGlobals();
});
async function mount() {
  await act(async () => root.render(<><HitlInteractionHandler agentId="dataagent" threadId="thread" /><Composer /></>));
}
async function choose() {
  await act(async () => host.querySelector<HTMLButtonElement>('section button')!.click());
}
async function typeAnswer(value: string) {
  await act(async () => {
    const textarea = host.querySelector('textarea')!;
    Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!.call(textarea, value);
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('submits a choice immediately from above the shared input without a second submit', async () => {
  await mount();
  const composer = host.querySelector('[data-testid="composer"]')!;
  expect(composer.querySelector('section')?.textContent).toContain('Which ID?');
  expect(host.querySelectorAll('textarea')).toHaveLength(1);
  expect(composer.querySelector('section textarea')).toBeNull();
  await choose();
  expect(mocks.runAgent).toHaveBeenCalledTimes(1);
  expect(mocks.runAgent).toHaveBeenCalledWith({
    agent: mocks.agent, forwardedProps: { hitlAnswers: { ask: 'user_id' } },
  });
  expect(mocks.getNativeSession).toHaveBeenCalledWith('thread');
  expect(host.querySelector('section')).toBeNull();
});

it('submits a custom answer using the same HITL path as the choices', async () => {
  await mount();
  await typeAnswer('Use account_id instead');
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(mocks.runAgent).toHaveBeenCalledWith({
    agent: mocks.agent, forwardedProps: { hitlAnswers: { ask: 'Use account_id instead' } },
  });
  expect(host.querySelector('textarea')!.value).toBe('');
});

it('retains a failed answer for retry when refreshing pending interactions also fails', async () => {
  mocks.runAgent.mockRejectedValue(new Error('Connection lost'));
  mocks.getNativeSession.mockRejectedValue(new Error('Offline'));
  await mount();
  await typeAnswer('custom answer');
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('Connection lost');
  expect(getHitlPending('thread')).toEqual([pending]);
  expect(host.querySelector('textarea')!.value).toBe('custom answer');
  expect(host.querySelector<HTMLButtonElement>('section button')!.disabled).toBe(false);
});

it('blocks duplicate choices and shared-input submissions until the request settles', async () => {
  let finish!: () => void;
  mocks.runAgent.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
  await mount();
  await act(async () => {
    const option = host.querySelector<HTMLButtonElement>('section button')!;
    option.click();
    option.click();
    host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
  expect(mocks.runAgent).toHaveBeenCalledTimes(1);
  expect(host.querySelector<HTMLButtonElement>('section button')!.disabled).toBe(true);
  expect(host.querySelector('textarea')!.disabled).toBe(true);
  await act(async () => finish());
});

it('keeps choices disabled while the agent is still running', async () => {
  mocks.agent.isRunning = true;
  await mount();
  await choose();
  expect(mocks.runAgent).not.toHaveBeenCalled();
});

it('closes the answered question on RUN_STARTED without waiting for the resumed stream', async () => {
  let finish!: () => void;
  mocks.runAgent.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
  await mount();
  await typeAnswer('user_id');
  await act(async () => host.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
  expect(host.querySelector('section')).not.toBeNull();
  mocks.agent.isRunning = true;
  await act(async () => acceptAnswer());
  expect(host.querySelector('section')).toBeNull();
  expect(getHitlPending('thread')).toEqual([]);
  expect(host.querySelector('textarea')!.value).toBe('');
  expect(mocks.getNativeSession).not.toHaveBeenCalled();
  await act(async () => finish());
});

it('removes only the submitted interrupt and restores a rejected answer from SSE', async () => {
  let finish!: () => void;
  mocks.runAgent.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
  const other = { ...pending, interruptId: 'second', message: 'Second question' };
  setHitlPending('thread', [pending, other]);
  await mount();
  await choose();
  mocks.agent.isRunning = true;
  await act(async () => acceptAnswer());
  expect(getHitlPending('thread')).toEqual([other]);
  expect(host.textContent).toContain('Second question');
  const retry = { ...pending, validationError: 'Please clarify' };
  await act(async () => {
    for (const subscriber of subscribers) {
      await subscriber.onCustomEvent?.({ ...answerRunParams(),
        event: { type: 'CUSTOM', name: 'human_feedback', value: [retry, other] } });
    }
  });
  expect(host.textContent).toContain('Please clarify');
  expect(host.textContent).toContain('Which ID?');
  mocks.getNativeSession.mockResolvedValue({ pendingInteractions: [retry, other] });
  await act(async () => finish());
});

it('keeps the question retryable when the SDK reports a rejected request without throwing', async () => {
  mocks.runAgent.mockImplementation(async () => {
    for (const subscriber of subscribers) {
      await subscriber.onRunFailed?.({ ...answerRunParams(), error: new Error('HTTP 409') });
    }
  });
  mocks.getNativeSession.mockRejectedValue(new Error('Offline'));
  await mount();
  await choose();
  expect(getHitlPending('thread')).toEqual([pending]);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain('HTTP 409');
  expect(host.querySelector<HTMLButtonElement>('section button')!.disabled).toBe(false);
});

it('does not acknowledge a different run or clear text typed after the accepted answer', async () => {
  let finish!: () => void;
  mocks.runAgent.mockReturnValue(new Promise<void>(resolve => { finish = resolve; }));
  await mount();
  await choose();
  await act(async () => acceptAnswer({ hitlAnswers: { other: 'user_id' } }));
  expect(getHitlPending('thread')).toEqual([pending]);
  await act(async () => acceptAnswer());
  await typeAnswer('next question');
  await act(async () => finish());
  expect(host.querySelector('textarea')!.value).toBe('next question');
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
  await choose();
  expect(mocks.runAgent).toHaveBeenCalledWith({
    agent: mocks.agent, forwardedProps: { hitlAnswers: { next: 'user_id' } },
  });
});
