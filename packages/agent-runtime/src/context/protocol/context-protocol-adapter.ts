export interface ContextProtocolAdapter<TView, TProtocol> {
  readonly protocol: string;
  toProtocol(view: TView): TProtocol;
}
