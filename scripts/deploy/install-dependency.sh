#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-}"
NON_INTERACTIVE=0
if [[ "${2:-}" == "--non-interactive" ]] || [[ "${DEPLOY_NON_INTERACTIVE:-}" == "1" ]]; then
  NON_INTERACTIVE=1
fi

usage() {
  echo "Usage: install-dependency.sh <node> [--non-interactive]" >&2
  exit 2
}

case "${ACTION}" in
  node) ;;
  *) usage ;;
esac

run_privileged() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif [[ "${NON_INTERACTIVE}" -eq 1 ]]; then
    sudo -n "$@"
  else
    sudo "$@"
  fi
}

install_node() {
  local setup
  local nodesource_url="https://deb.nodesource.com/setup_22.x"
  setup="$(mktemp)"
  trap 'rm -f "${setup}"' RETURN
  curl -fsSL "${nodesource_url}" -o "${setup}"
  run_privileged bash "${setup}"
  run_privileged apt-get install -y nodejs
  rm -f "${setup}"
  trap - RETURN
  node --version >/dev/null
  npm --version >/dev/null
}

case "${ACTION}" in
  node) install_node ;;
esac
