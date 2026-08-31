#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
STEP_START="${STEP_START:-1}"
STEP_END="${STEP_END:-6}"

readonly OPERATORS=(
  "step3_1_y_label_split.py"
  "step3_2_univariate_screening.py"
  "step3_3_feature_filter.py"
  "step3_4_train_model.py"
  "step3_5_white_box_model.py"
  "step3_6_white_box_scorecard_model.py"
)

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: required environment variable is empty: ${name}" >&2
    exit 2
  fi
}

validate_step_range() {
  if [[ ! "${STEP_START}" =~ ^[1-6]$ || ! "${STEP_END}" =~ ^[1-6]$ ]]; then
    echo "ERROR: STEP_START and STEP_END must be integers from 1 to 6" >&2
    exit 2
  fi
  if (( STEP_START > STEP_END )); then
    echo "ERROR: STEP_START cannot exceed STEP_END" >&2
    exit 2
  fi
}

assert_input_wide_table() {
  local path="${OUTPUT_DIR%/}/step2_4_wide_userfiltered.csv"
  if [[ ! -f "${path}" ]]; then
    echo "ERROR: missing model input wide table: ${path}" >&2
    echo "Feature engineering must export step2_4_wide_userfiltered.csv before model training" >&2
    exit 2
  fi
  if [[ ! -s "${path}" ]]; then
    echo "ERROR: model input wide table is empty: ${path}" >&2
    exit 2
  fi
}

snapshot_python_operators() {
  local operator
  for operator in "${OPERATORS[@]}"; do
    sha256sum "${SCRIPT_DIR}/${operator}"
  done
}

assert_operator_set() {
  local actual expected
  actual="$(
    for path in "${SCRIPT_DIR}"/step3_*.py; do
      [[ -e "${path}" ]] && basename "${path}"
    done | LC_ALL=C sort
  )"
  expected="$(printf '%s\n' "${OPERATORS[@]}" | LC_ALL=C sort)"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "ERROR: Python operator set changed; creating or removing Python operators is forbidden" >&2
    diff <(printf '%s\n' "${expected}") <(printf '%s\n' "${actual}") >&2 || true
    exit 3
  fi
}

run_operator() {
  local step="$1"
  local operator="${OPERATORS[$((step - 1))]}"
  echo "Running step3_${step} with fixed operator ${operator}"
  "${PYTHON_BIN}" "${SCRIPT_DIR}/${operator}"
}

require_env USER_ID_COL
require_env LABEL_COL
require_env OUTPUT_DIR
validate_step_range
assert_input_wide_table
assert_operator_set

before_hashes="$(snapshot_python_operators)"
for ((step = STEP_START; step <= STEP_END; step++)); do
  run_operator "${step}"
done
after_hashes="$(snapshot_python_operators)"

if [[ "${before_hashes}" != "${after_hashes}" ]]; then
  echo "ERROR: a fixed Python operator was modified during execution" >&2
  exit 3
fi

assert_operator_set
echo "step3 pipeline completed without creating or modifying Python operators"
