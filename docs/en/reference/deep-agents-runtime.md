# Deep Agents Runtime contract

This is the only interface between the DataFoundry control plane and an independent agent runtime. Web, TUI, and REST stay runtime-agnostic. The runtime stays DataFoundry-agnostic.

v1 covers conversation, streaming, cancel, HITL, and history persistence. Data tools, SQL audit, artifacts, semantic governance, and skills are out of scope.

The Python sidecar in `services/deepagents-runtime` implements the contract with Deep Agents `create_deep_agent`. `npm run dev` starts it on `:8790`. Without `LLM_API_KEY` it uses a scripted model on the real LangGraph path. Verify with `npm run smoke:deepagents-sdk`.

See the Chinese document for the full field tables and examples: [Deep Agents Runtime 接入契约](../../zh/reference/deep-agents-runtime.md).

Capability snapshot for runtime implementers (must / leftover / out of scope): [v1 capability boundary](deep-agents-runtime-boundary.md). Chinese source: [v1 能力边界](../../zh/reference/deep-agents-runtime-boundary.md).
