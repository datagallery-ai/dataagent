#!/usr/bin/env bash
# Shared bootstrap helpers for native deploy (sourced by ./deploy.sh).

check_supported_system() {
  local os_release="${DATAFOUNDRY_OS_RELEASE_FILE:-/etc/os-release}"
  if [[ ! -r "${os_release}" ]]; then
    echo "Unsupported operating system: ${os_release} is missing. DataFoundry native deploy supports Ubuntu/Debian only." >&2
    exit 1
  fi
  # shellcheck disable=SC1090,SC1091
  source "${os_release}"
  case "${ID:-}" in
    ubuntu|debian) ;;
    *)
      echo "Unsupported operating system: ${ID:-unknown}. DataFoundry native deploy supports Ubuntu/Debian only." >&2
      exit 1
      ;;
  esac

  local arch
  arch="${DATAFOUNDRY_UNAME_M:-$(uname -m)}"
  case "${arch}" in
    x86_64|amd64|aarch64|arm64) ;;
    *)
      echo "Unsupported architecture: ${arch}. DataFoundry native deploy supports x86_64/amd64 and aarch64/arm64 only." >&2
      exit 1
      ;;
  esac
}

command_is_readonly() {
  local token
  for token in "$@"; do
    case "${token}" in
      status|logs|stop|doctor|help) return 0 ;;
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

node_major_version() {
  local version
  version="$(node --version 2>/dev/null || true)"
  if [[ "${version}" =~ ^v([0-9]+) ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
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
  local setup
  local nodesource_url="https://deb.nodesource.com/setup_22.x"
  echo "Node.js 22 is required."
  echo "Installer source: ${nodesource_url}"
  echo "Commands:"
  echo "  curl -fsSL ${nodesource_url} -o /tmp/nodesource_setup.sh"
  echo "  bash /tmp/nodesource_setup.sh"
  echo "  apt-get install -y nodejs"

  if has_non_interactive_flag "$@"; then
    if ! can_install_noninteractive; then
      echo "Non-interactive Node.js installation requires root or passwordless sudo." >&2
      exit 1
    fi
  else
    local answer
    read -r -p "Install Node.js 22 from NodeSource now? [y/N]: " answer
    case "${answer}" in
      y|Y|yes|YES) ;;
      *)
        echo "Node.js 22 is required. Install it and re-run ./deploy.sh." >&2
        exit 1
        ;;
    esac
  fi

  setup="$(mktemp)"
  trap 'rm -f "${setup}"' RETURN
  curl -fsSL "${nodesource_url}" -o "${setup}"
  if [[ "$(id -u)" -eq 0 ]]; then
    bash "${setup}"
    apt-get install -y nodejs
  elif has_non_interactive_flag "$@"; then
    sudo -n bash "${setup}"
    sudo -n apt-get install -y nodejs
  else
    sudo bash "${setup}"
    sudo apt-get install -y nodejs
  fi
  rm -f "${setup}"
  trap - RETURN

  if ! node --version >/dev/null 2>&1 || ! npm --version >/dev/null 2>&1; then
    echo "Node.js 22 installation did not produce working node/npm commands." >&2
    exit 1
  fi
  local major
  major="$(node_major_version || true)"
  if [[ -z "${major}" || "${major}" -lt 22 ]]; then
    echo "Node.js 22+ is required after installation; found $(node --version 2>/dev/null || echo none)." >&2
    exit 1
  fi
}

ensure_node_22() {
  local major=""
  if command -v node >/dev/null 2>&1; then
    major="$(node_major_version || true)"
  fi

  if [[ -n "${major}" && "${major}" -ge 22 ]]; then
    return 0
  fi

  if command_is_readonly "$@"; then
    echo "Node.js 22+ is required for this command. Install Node.js 22 and re-run ./deploy.sh $*." >&2
    exit 1
  fi

  if [[ -n "${major}" && "${major}" -lt 22 ]]; then
    echo "Unsupported Node.js version: $(node --version). DataFoundry requires Node.js 22.x." >&2
  fi

  install_node_22 "$@"
}
