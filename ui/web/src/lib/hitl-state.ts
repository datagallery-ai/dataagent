import type { PendingInteraction } from './config-api/types';
const empty: PendingInteraction[] = [];
const pending = new Map<string, PendingInteraction[]>();
const listeners = new Set<() => void>();
export function setHitlPending(thread: string, value: PendingInteraction[]) {
  pending.set(thread, value);
  listeners.forEach(listener => listener());
}
export function getHitlPending(thread: string) { return pending.get(thread) ?? empty; }
export function subscribeHitl(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function parseHitlPending(value: unknown): PendingInteraction[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is PendingInteraction => v?.type === 'human_feedback'
    && typeof v.interruptId === 'string' && typeof v.message === 'string'
    && (v.options === undefined || (Array.isArray(v.options) && v.options.every((option: unknown) => typeof option === 'string')))
    && (v.validationError === undefined || typeof v.validationError === 'string'));
}
