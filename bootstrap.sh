#!/usr/bin/env bash
# bootstrap.sh -- one-line installer for mdt-tools.
#
# Designed to be piped from curl. Installs git, clones the repo into
# /opt/mdt-tools-src/, then hands off to install.sh which does the
# heavy lifting (apt deps, boot config, systemd units, pymcuprog venv).
#
# Usage on a fresh Pi (Raspberry Pi OS Lite, Bookworm or later):
#
#   curl -sSL https://gitlab.com/masterdatatool/software/mdtv2-tools/-/raw/master/bootstrap.sh | sudo bash
#
# Env overrides:
#   REPO_URL   Git URL to clone        (default: gitlab.com mdtv2-tools)
#   BRANCH     Branch to check out     (default: master)
#   SRC_DIR    Where to put the clone  (default: /opt/mdt-tools-src)
#
# Re-running is safe: an existing SRC_DIR is fetched + reset to the
# requested branch instead of re-cloned, and install.sh is itself
# idempotent.
set -euo pipefail

REPO_URL="${REPO_URL:-https://gitlab.com/masterdatatool/software/mdtv2-tools.git}"
SRC_DIR="${SRC_DIR:-/opt/mdt-tools-src}"
BRANCH="${BRANCH:-master}"

# ---------- logging ----------------------------------------------------------
note() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m  %s\n' "$*"; }
err()  { printf '\033[1;31m  !!\033[0m %s\n' "$*"  >&2; }

# ---------- preflight --------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "must run as root (pipe through sudo:"
    err "  curl -sSL <url> | sudo bash)"
    exit 1
fi

# ---------- 1. git -----------------------------------------------------------
if ! command -v git >/dev/null 2>&1; then
    note "installing git"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates
    ok "git installed"
else
    ok "git already present"
fi

# ---------- 2. clone (or update) the repo -----------------------------------
if [[ -d "$SRC_DIR/.git" ]]; then
    note "updating existing clone at $SRC_DIR"
    git -C "$SRC_DIR" fetch --quiet origin "$BRANCH"
    git -C "$SRC_DIR" checkout --quiet "$BRANCH"
    git -C "$SRC_DIR" reset --hard --quiet "origin/$BRANCH"
else
    note "cloning $REPO_URL (branch: $BRANCH) -> $SRC_DIR"
    mkdir -p "$(dirname "$SRC_DIR")"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi
ok "code at $SRC_DIR"

# ---------- 3. hand off to install.sh ---------------------------------------
if [[ ! -x "$SRC_DIR/install.sh" ]]; then
    chmod +x "$SRC_DIR/install.sh"
fi

note "handing off to $SRC_DIR/install.sh"
exec "$SRC_DIR/install.sh"
