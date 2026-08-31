export interface ContextProtocolEventSink {
  emitContextEvent(name: string, value: unknown): void;
}
