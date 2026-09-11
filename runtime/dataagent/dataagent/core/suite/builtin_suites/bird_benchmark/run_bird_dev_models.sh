#!/usr/bin/env bash
# Compatibility launcher; orchestration lives in run_bird.py.
set -Eeuo pipefail
PYTHON_BIN="${PYTHON_BIN:-python3}"
command=run
case "${1:-}" in
  run|prepare|verify|import|check|retry|aggregate) command="$1"; shift ;;
esac
args=()
[[ -z "${BIRD_DATA_DIR:-}" ]] || args+=(--bird-data-dir "$BIRD_DATA_DIR")
[[ -z "${TRAIN_CACHE:-}" ]] || args+=(--train-cache "$TRAIN_CACHE")
[[ -z "${ENV_FILE:-}" ]] || args+=(--api-key-file "$ENV_FILE")
[[ -z "${RUNS_ROOT:-}" ]] || args+=(--run-dir "$RUNS_ROOT")
[[ -z "${SEMANTIC_SERVICE_URL:-}" ]] || args+=(--semantic-service-url "$SEMANTIC_SERVICE_URL")
[[ -z "${SEMANTIC_DB_PREFIX:-}" ]] || args+=(--semantic-db-prefix "$SEMANTIC_DB_PREFIX")
[[ -z "${SEMANTIC_PREPROCESS_ROOT:-}" ]] || args+=(--preprocess-root "$SEMANTIC_PREPROCESS_ROOT")
[[ -z "${SEMANTIC_PREP_MODE:-}" ]] || args+=(--preprocess "$SEMANTIC_PREP_MODE")
[[ -z "${FULL_CASE_TIMEOUT:-}" ]] || args+=(--case-timeout "$FULL_CASE_TIMEOUT")
[[ -z "${RUNTIME_LLM_TIMEOUT:-}" ]] || args+=(--llm-timeout "$RUNTIME_LLM_TIMEOUT")
[[ -z "${RUNTIME_LLM_NUM_RETRIES:-}" ]] || args+=(--llm-num-retries "$RUNTIME_LLM_NUM_RETRIES")
if [[ -n "${THINKING_MODE:-}" ]]; then
  args+=(--thinking "$THINKING_MODE")
  case "$THINKING_MODE" in
    enabled) args+=(--enable-thinking true) ;;
    disabled) args+=(--enable-thinking false) ;;
    omit) args+=(--enable-thinking omit) ;;
    *) echo "THINKING_MODE must be enabled, disabled or omit" >&2; exit 2 ;;
  esac
fi
case "${RERUN_FAILURES_ONCE:-1}" in
  0) args+=(--no-retry) ;;
  1) args+=(--retry) ;;
  *) echo "RERUN_FAILURES_ONCE must be 0 or 1" >&2; exit 2 ;;
esac
if [[ -n "${RUN_GLM:-}${RUN_DEEPSEEK:-}" ]]; then
  case "${RUN_GLM:-0},${RUN_DEEPSEEK:-0}" in
    1,0) args+=(--model GLM-5.2) ;;
    0,1) args+=(--model DeepSeek-V4-Flash-0731) ;;
    *) echo "Run one model per invocation; use separate --model/--run-dir values for model comparisons" >&2; exit 2 ;;
  esac
fi
exec "$PYTHON_BIN" -m dataagent.core.suite.builtin_suites.bird_benchmark.run_bird "$command" "${args[@]}" "$@"
