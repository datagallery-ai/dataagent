"use client";
import { useAgent, useCopilotKit } from '@copilotkit/react-core/v2';
import React, { useEffect, useState, useSyncExternalStore } from 'react';
import { configApi } from '../../../../lib/config-api/client';
import { getHitlPending, parseHitlPending, setHitlPending, subscribeHitl } from '../../../../lib/hitl-state';

export function HitlInteractionHandler({ agentId, threadId }: { agentId: string; threadId: string }) {
  const { agent } = useAgent({ agentId });
  const { copilotkit } = useCopilotKit();
  const pending = useSyncExternalStore(subscribeHitl, () => getHitlPending(threadId), () => getHitlPending(threadId));
  const [answer, setAnswer] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    const subscription = agent.subscribe({
      onCustomEvent: ({ event }) => {
        if (event.name === 'human_feedback') setHitlPending(threadId, parseHitlPending(event.value));
      },
    });
    return () => subscription.unsubscribe();
  }, [agent, threadId]);
  useEffect(() => { setAnswer(''); setError(''); }, [threadId, pending[0]?.interruptId]);
  const current = pending[0];
  if (!current) return null;
  async function submit() {
    if (!current || !answer.trim() || submitting || agent.isRunning) return;
    setSubmitting(true); setError('');
    try {
      await copilotkit.runAgent({ agent, forwardedProps: { hitlAnswers: { [current.interruptId]: answer } } });
      setAnswer('');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      try {
        const session = await configApi.getNativeSession(threadId);
        setHitlPending(threadId, session.pendingInteractions ?? []);
      } catch { /* Keep the pending question available for a retry. */ }
      setSubmitting(false);
    }
  }
  return <form className="m-3 rounded-lg border p-4" onSubmit={e => { e.preventDefault(); void submit(); }} aria-label="人工反馈">
    <p className="whitespace-pre-wrap">{current.message}</p>
    {pending.length > 1 && <p>还有 {pending.length - 1} 个问题待回答</p>}
    <div className="my-2 flex flex-wrap gap-2">{current.options?.map(option =>
      <button key={option} type="button" className="rounded border px-2 py-1" onClick={() => setAnswer(option)}>{option}</button>)}</div>
    {(current.validationError || error) && <p role="alert">{error || current.validationError}</p>}
    <textarea aria-label="回答" value={answer} onChange={e => setAnswer(e.target.value)} disabled={submitting} className="w-full rounded border p-2" />
    <button type="submit" disabled={submitting || agent.isRunning || !answer.trim()} className="mt-2 rounded border px-3 py-1">{submitting ? '提交中…' : '提交回答'}</button>
  </form>;
}
