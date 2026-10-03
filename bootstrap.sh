#!/usr/bin/env bash
# bootstrap.sh -- one-line installer for mdt-tools.
#
# Designed to be piped from curl. Installs git, clones the repo into
# /opt/mdt-tools-src/, then hands off to install.sh which does the
# heavy lifting (apt deps, boot config, systemd units, pymcuprog venv).
#
# Usage on a fresh Pi (Raspberry Pi OS Lite, Bookworm or later):
#
#   curl -sSL https://raw.githubusercontent.com/tb59427/mdtv2-tools/master/bootstrap.sh | sudo bash
#
# This is the TB fork of Philip Voigt's mdtv2-tools (see CHANGES.md); for
# the original use REPO_URL=https://gitlab.com/masterdatatool/software/mdtv2-tools.git
#
# Env overrides:
#   REPO_URL   Git URL to clone        (default: github.com/tb59427/mdtv2-tools)
#   BRANCH     Branch to check out     (default: master)
#   SRC_DIR    Where to put the clone  (default: /opt/mdt-tools-src)
#
# Re-running is safe: an existing SRC_DIR is fetched + reset to the
# requested branch instead of re-cloned, and install.sh is itself
# idempotent. If that clone points at a different REPO_URL (e.g. an
# install made from the original GitLab repo), its origin is switched
# over first. Local edits in SRC_DIR are discarded by the reset.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/tb59427/mdtv2-tools.git}"
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
    current_url=$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)
    if [[ $current_url != "$REPO_URL" ]]; then
        note "switching origin: ${current_url:-<none>} -> $REPO_URL"
        git -C "$SRC_DIR" remote set-url origin "$REPO_URL" 2>/dev/null \
            || git -C "$SRC_DIR" remote add origin "$REPO_URL"
    fi
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
