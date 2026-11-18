#!/usr/bin/env bash
# Shared bootstrap helpers for native deploy (sourced by ./deploy.sh).

MIN_NODE_VERSION="22.12.0"

command_skips_install() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --config|--runtime-url|--web-host|--web-port)
        shift
        if [[ "$#" -gt 0 ]]; then shift; fi
        ;;
      start|restart|status|logs|stop|doctor|help) return 0 ;;
      deploy|tui) return 1 ;;
      *) shift ;;
    esac
  done
  return 1
}

has_non_interactive_flag() {
  local token
  for token in "$@"; do
    [[ "${token}" == "--non-interactive" ]] && return 0
  done
  return 1
}

node_version_supported() {
  local version="${1#v}" major minor patch minimum_major minimum_minor minimum_patch
  [[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  IFS=. read -r major minor patch <<< "${version}"
  IFS=. read -r minimum_major minimum_minor minimum_patch <<< "${MIN_NODE_VERSION}"
  (( major > minimum_major || (major == minimum_major &&
    (minor > minimum_minor || (minor == minimum_minor && patch >= minimum_patch))) ))
}

can_install_noninteractive() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return 0
  fi
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

install_node_22() {
  local installer
  installer="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/install-dependency.sh"
  echo "Node.js ${MIN_NODE_VERSION}+ is required."
  echo "Installer: bash ${installer} node (DATAAGENT_NODE_PACKAGE_URL or NodeSource; apt-get, dnf or yum)."

  if has_non_interactive_flag "$@"; then
    if ! can_install_noninteractive; then
      echo "Non-interactive Node.js installation requires root or passwordless sudo." >&2
      exit 1
    fi
  else
    local answer
    read -r -p "Install Node.js ${MIN_NODE_VERSION}+ now? [y/N]: " answer
    case "${answer}" in
      y|Y|yes|YES) ;;
      *)
        echo "Node.js ${MIN_NODE_VERSION}+ is required. Install it and re-run ./deploy.sh." >&2
        exit 1
        ;;
    esac
  fi

  if has_non_interactive_flag "$@"; then
    bash "${installer}" node --non-interactive
  else
    bash "${installer}" node
  fi
}

ensure_node_22() {
  local version
  version="$(node --version 2>/dev/null || true)"
  if node_version_supported "${version}"; then
    return 0
  fi

  if command_skips_install "$@"; then
    echo "Node.js ${MIN_NODE_VERSION}+ is required for this command. Install it and re-run ./deploy.sh $*." >&2
    exit 1
  fi

  if [[ -n "${version}" ]]; then
    echo "Unsupported Node.js version: ${version}. DataGallery requires Node.js ${MIN_NODE_VERSION}+." >&2
  fi

  install_node_22 "$@"
}
