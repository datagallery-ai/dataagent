#!/usr/bin/env bash
set -Eeuo pipefail
# Share the same version guard as ./deploy.sh, including before Node is available.
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/bootstrap.sh"

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

install_node() (
  local manager="" candidate version
  for candidate in apt-get dnf yum; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      manager="${candidate}"
      break
    fi
  done
  if [[ -z "${manager}" ]]; then
    echo "Cannot install Node.js ${MIN_NODE_VERSION}+: no supported package manager found (apt-get, dnf, yum). Install Node.js ${MIN_NODE_VERSION}+ and npm manually, then retry." >&2
    exit 1
  fi

  if [[ -n "${DATAAGENT_NODE_PACKAGE_URL:-}" ]]; then
    local package extension="rpm"
    [[ "${manager}" != "apt-get" ]] || extension="deb"
    package="$(mktemp --suffix=".${extension}")"
    trap 'rm -f "${package}"' EXIT
    echo "Installing Node.js from DATAAGENT_NODE_PACKAGE_URL using ${manager}."
    curl -fsSL --connect-timeout 10 --max-time 300 "${DATAAGENT_NODE_PACKAGE_URL}" -o "${package}"
    # apt downloads run as _apt, which must be able to read this temporary package.
    chmod 644 "${package}"
    run_privileged "${manager}" install -y "${package}"
  else
    echo "Installing Node.js 22.x (minimum ${MIN_NODE_VERSION}) from NodeSource using ${manager}."
    if [[ "${manager}" == "apt-get" ]]; then
      local setup
      setup="$(mktemp)"
      trap 'rm -f "${setup}"' EXIT
      curl -fsSL https://deb.nodesource.com/setup_22.x -o "${setup}"
      run_privileged bash "${setup}"
    else
      # Configure the RPM repository directly without the upstream script's distribution checks.
      cat <<'REPO' | run_privileged tee /etc/yum.repos.d/nodesource-nodejs.repo >/dev/null
[nodesource-nodejs]
name=Node.js 22.x
baseurl=https://rpm.nodesource.com/pub_22.x/nodistro/nodejs/$basearch
enabled=1
gpgcheck=1
gpgkey=https://rpm.nodesource.com/gpgkey/ns-operations-public.key
module_hotfixes=1
REPO
    fi
    run_privileged "${manager}" install -y nodejs
  fi
  if ! version="$(node --version)" || ! npm --version >/dev/null; then
    echo "Node.js ${MIN_NODE_VERSION}+ installation did not produce working node/npm commands." >&2
    exit 1
  fi
  if ! node_version_supported "${version}"; then
    echo "Node.js ${MIN_NODE_VERSION}+ is required after installation; found ${version}." >&2
    exit 1
  fi
)

case "${ACTION}" in
  node) install_node ;;
esac
