"use client";
import { useAgent, useCopilotKit } from '@copilotkit/react-core/v2';
import React, { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { configApi } from '../../../../lib/config-api/client';
import { getHitlPending, parseHitlPending, setHitlPending, subscribeHitl } from '../../../../lib/hitl-state';
import type { PendingInteraction } from '../../../../lib/config-api/types';
import { useT } from '../../../../i18n/locale-context';

export function HitlInteractionHandler({ agentId, threadId }: { agentId: string; threadId: string }) {
  const { agent } = useAgent({ agentId });
  useEffect(() => {
    const subscription = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name === 'human_feedback') setHitlPending(threadId, parseHitlPending(event.value));
      },
    });
    return () => subscription.unsubscribe();
  }, [agent, threadId]);
  return null;
}

export function useHitlInteraction(agentId: string, threadId: string | null) {
  const { agent } = useAgent({ agentId });
  const { copilotkit } = useCopilotKit();
  const pending = useSyncExternalStore(subscribeHitl,
    () => getHitlPending(threadId ?? ''), () => getHitlPending(threadId ?? ''));
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);
  const activeThread = useRef(threadId);
  activeThread.current = threadId;
  const current = pending[0];
  useEffect(() => { setError(''); }, [threadId, current?.interruptId]);

  async function submitAnswer(answer: string): Promise<boolean> {
    if (!threadId || !current || !answer.trim() || inFlight.current || agent.isRunning) return false;
    inFlight.current = true;
    setSubmitting(true);
    setError('');
    let accepted = false;
    let failed = false;
    let acknowledge!: (submitted: boolean) => void;
    const acknowledgement = new Promise<boolean>(resolve => { acknowledge = resolve; });
    const isAnswerRun = (input: { threadId: string; forwardedProps?: Record<string, unknown> }) =>
      input.threadId === threadId &&
      (input.forwardedProps?.hitlAnswers as Record<string, string> | undefined)?.[current.interruptId] === answer;
    const fail = (error: unknown) => {
      failed = true;
      if (!accepted && activeThread.current === threadId) {
        setError(error instanceof Error ? error.message : String(error));
      }
    };
    const subscription = agent.subscribe({
      onRunStartedEvent: ({ input }) => {
        if (!isAnswerRun(input) || accepted) return;
        // The server validates resume IDs before RUN_STARTED. The stream can
        // continue for minutes; the answered question should close immediately.
        accepted = true;
        setHitlPending(threadId, getHitlPending(threadId).filter(item => item.interruptId !== current.interruptId));
        if (activeThread.current === threadId) setSubmitting(false);
        acknowledge(true);
      },
      // CopilotKit may report a failed run via subscribers and resolve its promise.
      onRunFailed: ({ input, error }) => { if (isAnswerRun(input)) fail(error); },
      onRunErrorEvent: ({ input, event }) => { if (isAnswerRun(input)) fail(event.message); },
    });
    void (async () => {
      try {
        await copilotkit.runAgent({ agent, forwardedProps: { hitlAnswers: { [current.interruptId]: answer } } });
      } catch (error) {
        fail(error);
      } finally {
        subscription.unsubscribe();
        try {
          const session = await configApi.getNativeSession(threadId);
          setHitlPending(threadId, session.pendingInteractions ?? []);
        } catch { /* Keep unresolved questions available for retry or REST recovery. */ }
        inFlight.current = false;
        if (activeThread.current === threadId) setSubmitting(false);
        acknowledge(accepted || !failed);
      }
    })();
    return acknowledgement;
  }
  return { pending, current, error, submitting, disabled: submitting || agent.isRunning, submitAnswer };
}

export function HitlQuestion({ interaction, pendingCount, disabled, error, onAnswer }: {
  interaction: PendingInteraction;
  pendingCount: number;
  disabled: boolean;
  error: string;
  onAnswer: (answer: string) => void;
}) {
  const t = useT();
  return <section aria-label={t('chatInput.feedbackTitle')} aria-live="polite"
    className="border-b border-border px-4 py-3">
    <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted">
      <span className="h-1.5 w-1.5 rounded-full bg-primary" aria-hidden />
      {t('chatInput.feedbackTitle')}
      {pendingCount > 1 && <span>{t('chatInput.feedbackRemaining', { count: pendingCount - 1 })}</span>}
    </div>
    <p className="max-h-48 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-6 text-foreground">{interaction.message}</p>
    {!!interaction.options?.length && <div className="mt-3 flex flex-wrap gap-2">
      {interaction.options.map((option, index) => <button key={`${index}:${option}`} type="button"
        disabled={disabled} onClick={() => onAnswer(option)}
        className="cursor-pointer whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-subtle px-3 py-1.5 text-left text-sm text-foreground transition-colors hover:border-primary hover:bg-primary/8 focus-visible:outline-2 focus-visible:outline-primary disabled:cursor-default disabled:opacity-50">
        {option}
      </button>)}
    </div>}
    {(error || interaction.validationError) && <p role="alert" className="mt-2 text-xs text-danger">{error || interaction.validationError}</p>}
  </section>;
}
