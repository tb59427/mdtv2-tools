#!/usr/bin/env bash
# install.sh -- one-command Raspberry Pi setup for mdt-tools.
#
# Idempotent. Safe to re-run after pulling new code; it will only
# replace files that have actually changed and only restart services
# that depend on them.
#
# Run from the repo root on the Pi:
#
#     sudo ./install.sh
#
# What it does:
#   1. Installs apt dependencies (redis-server, python3-redis, shairport-sync, ...)
#   2. Patches /boot/firmware/config.txt:
#        - enable_uart=1
#        - dtoverlay=disable-bt
#        - dtparam=audio=off                (disables HDMI/3.5mm audio)
#        - dtoverlay=hifiberry-dacplusadc   (enables HiFiBerry HAT)
#   3. Patches /boot/firmware/cmdline.txt to drop the serial console.
#   4. Disables conflicting services (serial-getty, hciuart).
#   5. Copies code to /opt/mdt-tools/{broker,ml-source-bridge,ml-debug,mcu-firmware}.
#   6. Installs systemd unit files + the shairport-sync D-Bus policy.
#   7. Sets up a hidden pymcuprog venv at /opt/mdt-tools/.pymcuprog-venv/
#      (used transparently by mcu-firmware/flash.sh).
#   8. Installs a default /etc/ml-source-bridge.toml (only if missing).
#   9. Reloads systemd / dbus / shairport-sync only if their files changed.
#  10. Prints a summary of next steps.
#
# After running, edit /etc/ml-source-bridge.toml, then:
#   sudo systemctl enable --now mdtv2-broker.service ml-source-bridge.service
#
# To flash the MCU:
#   sudo /opt/mdt-tools/mcu-firmware/flash.sh /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex

set -euo pipefail

# ---------- paths ------------------------------------------------------------
SCRIPT="$(realpath "${BASH_SOURCE[0]}")"
SOURCE_DIR="$(dirname "$SCRIPT")"

INSTALL_ROOT=/opt/mdt-tools
PYMCUPROG_VENV="$INSTALL_ROOT/.pymcuprog-venv"

CONFIG_TXT_PRIMARY=/boot/firmware/config.txt
CONFIG_TXT_FALLBACK=/boot/config.txt
CMDLINE_PRIMARY=/boot/firmware/cmdline.txt
CMDLINE_FALLBACK=/boot/cmdline.txt

BRIDGE_TOML=/etc/ml-source-bridge.toml
DBUS_POLICY=/etc/dbus-1/system.d/shairport-sync-instance-policy.conf
BROKER_UNIT=/etc/systemd/system/mdtv2-broker.service
BRIDGE_UNIT=/etc/systemd/system/ml-source-bridge.service

SERVICE_USER=mdt

# ---------- logging ----------------------------------------------------------
note() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m  %s\n' "$*"; }
skip() { printf '\033[1;90m skip\033[0m  %s\n' "$*"; }
warn() { printf '\033[1;33m warn\033[0m  %s\n' "$*" >&2; }
err()  { printf '\033[1;31m  !!\033[0m %s\n' "$*"  >&2; }

# ---------- preflight --------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    err "must run as root (try: sudo $0)"
    exit 1
fi

if [[ ! -f "$SOURCE_DIR/install.sh" || ! -d "$SOURCE_DIR/broker" ]]; then
    err "$SOURCE_DIR doesn't look like an mdt-tools checkout"
    exit 1
fi

# Pi sanity check (warn, don't abort -- might be useful elsewhere)
if ! grep -qiE 'raspberry|bcm' /proc/cpuinfo 2>/dev/null; then
    warn "this doesn't look like a Raspberry Pi -- continuing anyway"
fi

# Need Python 3.11+ for tomllib.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    err "Python 3.11+ required for ml-source-bridge (uses tomllib)"
    python3 --version
    exit 1
fi

# ---------- track work -------------------------------------------------------
NEED_DAEMON_RELOAD=0
NEED_DBUS_RELOAD=0
NEED_SHAIRPORT_RESTART=0
NEED_REBOOT=0
NEED_CONFIG_EDIT=0

# Pick which boot files exist.
CFG_TXT=$CONFIG_TXT_PRIMARY
[[ -f $CFG_TXT ]] || CFG_TXT=$CONFIG_TXT_FALLBACK
CMDLINE=$CMDLINE_PRIMARY
[[ -f $CMDLINE ]] || CMDLINE=$CMDLINE_FALLBACK

# ---------- 1. apt packages --------------------------------------------------
note "apt packages"
APT_PKGS=(
    rsync
    redis-server
    python3-redis
    python3-serial
    shairport-sync
    python3-venv          # for the hidden pymcuprog venv
)
# Note: `pinctrl` (used by mcu-firmware/flash.sh to toggle UART.SEL) is
# a binary that ships pre-installed on Raspberry Pi OS; it isn't an
# apt-installable package, so we don't try to install it here. The
# flasher will warn if it isn't on PATH.
MISSING=()
for p in "${APT_PKGS[@]}"; do
    if ! dpkg -s "$p" >/dev/null 2>&1; then
        MISSING+=("$p")
    fi
done
if (( ${#MISSING[@]} > 0 )); then
    note "installing: ${MISSING[*]}"
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING[@]}"
    ok "apt packages installed"
else
    skip "all apt packages already installed"
fi

if ! command -v pinctrl >/dev/null 2>&1; then
    warn "pinctrl not on PATH -- mcu-firmware/flash.sh won't be able to toggle"
    warn "UART.SEL until pinctrl is available. On stock RPi OS it ships by"
    warn "default; on other distros install rpi-gpio-tools or equivalent."
fi

# ---------- 2. service user --------------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    note "creating service user '$SERVICE_USER'"
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "user created"
else
    skip "service user '$SERVICE_USER' already exists"
fi
# Group memberships needed by the bridge / broker.
for g in dialout audio gpio i2c; do
    if getent group "$g" >/dev/null 2>&1; then
        if ! id -nG "$SERVICE_USER" | grep -qw "$g"; then
            usermod -aG "$g" "$SERVICE_USER"
            ok "added '$SERVICE_USER' to group '$g'"
        fi
    fi
done

# ---------- 3. boot config ---------------------------------------------------
note "boot config: $CFG_TXT"
if [[ ! -f $CFG_TXT ]]; then
    err "$CFG_TXT not found -- can't configure boot"
    exit 1
fi
if ! grep -qE '^# --- mdt-tools begin ---' "$CFG_TXT"; then
    note "appending mdt-tools block to $CFG_TXT"
    cp -p "$CFG_TXT" "$CFG_TXT.bak-$(date +%s)"
    cat "$SOURCE_DIR/system-config/boot-config.txt.snippet" >> "$CFG_TXT"
    NEED_REBOOT=1
    ok "boot config patched"
else
    skip "$CFG_TXT already has mdt-tools block"
fi

# Kill the HDMI audio card (vc4-hdmi). `dtparam=audio=off` only disables
# the legacy BCM2835 3.5mm jack; HDMI audio is exposed by the KMS/GPU
# driver via the vc4-kms-v3d overlay, which is enabled by Pi OS by
# default. The fix is to add `noaudio` as a parameter to that existing
# overlay line -- we can't add a second dtoverlay= line, we have to
# edit the existing one in place.
if grep -qE '^dtoverlay=vc4-kms-v3d(-pi5)?([,[:space:]]|$)' "$CFG_TXT" \
        && ! grep -qE '^dtoverlay=vc4-kms-v3d(-pi5)?[^#]*\bnoaudio\b' "$CFG_TXT"; then
    note "patching vc4-kms-v3d overlay with ,noaudio (kills vc4-hdmi card)"
    cp -p "$CFG_TXT" "$CFG_TXT.bak-$(date +%s)"
    # Insert ",noaudio" immediately after vc4-kms-v3d (or -pi5 variant),
    # preserving any other parameters already on the line.
    sed -i -E 's/^(dtoverlay=vc4-kms-v3d(-pi5)?)([,[:space:]]|$)/\1,noaudio\3/' "$CFG_TXT"
    NEED_REBOOT=1
    ok "vc4-kms-v3d patched to ,noaudio"
else
    skip "vc4-kms-v3d already has ,noaudio (or no such overlay line)"
fi

# Strip serial console from cmdline so it doesn't fight the broker.
note "boot cmdline: $CMDLINE"
if grep -qE 'console=(serial0|ttyAMA0|ttyS0),[0-9]+' "$CMDLINE"; then
    cp -p "$CMDLINE" "$CMDLINE.bak-$(date +%s)"
    sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' "$CMDLINE"
    NEED_REBOOT=1
    ok "stripped serial console from cmdline"
else
    skip "no serial console in cmdline"
fi

# ---------- 4. disable conflicting services ---------------------------------
for svc in serial-getty@ttyAMA0 serial-getty@ttyS0 hciuart ml-broker; do
    if systemctl list-unit-files "$svc.service" >/dev/null 2>&1 \
       && systemctl is-enabled --quiet "$svc.service" 2>/dev/null; then
        note "disabling $svc"
        systemctl disable --now "$svc.service" 2>/dev/null || true
    fi
done

# ---------- 5. sync code -----------------------------------------------------
note "sync code -> $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"
RSYNC_OPTS=(-a --delete
    --exclude '__pycache__'
    --exclude '.git'
    --exclude '.DS_Store'
    --exclude 'config.toml'
    --exclude '.pymcuprog-venv'
)
for d in broker ml-source-bridge ml-debug mcu-firmware; do
    rsync "${RSYNC_OPTS[@]}" "$SOURCE_DIR/$d/" "$INSTALL_ROOT/$d/"
done
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT"
chmod 755 "$INSTALL_ROOT/mcu-firmware/flash.sh"
ok "code synced"

# ---------- 6. systemd unit files -------------------------------------------
note "systemd unit files"
for src_unit in "$SOURCE_DIR/broker/mdtv2-broker.service" \
                "$SOURCE_DIR/ml-source-bridge/ml-source-bridge.service"; do
    name=$(basename "$src_unit")
    dst="/etc/systemd/system/$name"
    if cmp -s "$src_unit" "$dst" 2>/dev/null; then
        skip "$name already up to date"
    else
        install -m 644 "$src_unit" "$dst"
        NEED_DAEMON_RELOAD=1
        ok "installed $name"
    fi
done

# ---------- 7. shairport-sync D-Bus policy ----------------------------------
SRC_POLICY="$SOURCE_DIR/ml-source-bridge/shairport-sync-instance-policy.conf"
if [[ -f $SRC_POLICY ]]; then
    if cmp -s "$SRC_POLICY" "$DBUS_POLICY" 2>/dev/null; then
        skip "shairport-sync D-Bus policy already up to date"
    else
        install -m 644 "$SRC_POLICY" "$DBUS_POLICY"
        NEED_DBUS_RELOAD=1
        NEED_SHAIRPORT_RESTART=1
        ok "shairport-sync D-Bus policy installed"
    fi
else
    warn "shairport policy file not found at $SRC_POLICY"
fi

# ---------- 8. default config ------------------------------------------------
if [[ -f $BRIDGE_TOML ]]; then
    skip "$BRIDGE_TOML already exists -- not touching"
else
    install -m 644 "$SOURCE_DIR/ml-source-bridge/config.toml.example" "$BRIDGE_TOML"
    NEED_CONFIG_EDIT=1
    ok "wrote default $BRIDGE_TOML (edit before enabling the service)"
fi

# ---------- 9. pymcuprog venv -----------------------------------------------
note "pymcuprog (hidden venv at $PYMCUPROG_VENV)"
if [[ ! -x $PYMCUPROG_VENV/bin/pymcuprog ]]; then
    python3 -m venv "$PYMCUPROG_VENV"
    "$PYMCUPROG_VENV/bin/pip" install --quiet --upgrade pip
    "$PYMCUPROG_VENV/bin/pip" install --quiet pymcuprog
    chown -R "$SERVICE_USER:$SERVICE_USER" "$PYMCUPROG_VENV"
    ok "pymcuprog installed"
else
    skip "pymcuprog venv already present"
fi

# ---------- 10. reloads ------------------------------------------------------
if (( NEED_DAEMON_RELOAD )); then
    note "systemctl daemon-reload"
    systemctl daemon-reload
fi
if (( NEED_DBUS_RELOAD )); then
    note "reloading dbus"
    systemctl reload dbus 2>/dev/null || warn "couldn't reload dbus"
fi
if (( NEED_SHAIRPORT_RESTART )); then
    note "restarting shairport-sync (claiming new D-Bus name)"
    systemctl restart shairport-sync 2>/dev/null \
        || warn "couldn't restart shairport-sync"
fi

# Enable + start (or restart) both services. Fresh install: enable+now.
# Update install: restart to pick up new code.
for svc in mdtv2-broker.service ml-source-bridge.service; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        note "restarting $svc"
        systemctl restart "$svc"
    else
        note "enable --now $svc"
        systemctl enable --now "$svc"
    fi
done

# ---------- summary ----------------------------------------------------------
echo
note "install complete"
echo
echo "  services: $(systemctl is-active mdtv2-broker.service 2>/dev/null) mdtv2-broker, $(systemctl is-active ml-source-bridge.service 2>/dev/null) ml-source-bridge"
echo "  logs:     sudo journalctl -u mdtv2-broker.service -u ml-source-bridge.service -f"
if (( NEED_CONFIG_EDIT )); then
    echo
    echo "  /etc/ml-source-bridge.toml was created with default values --"
    echo "  edit it if you need to change source mappings / display names,"
    echo "  then: sudo systemctl restart ml-source-bridge.service"
fi
echo
echo "  flash MCU: sudo /opt/mdt-tools/mcu-firmware/flash.sh \\"
echo "             /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex"
echo
echo "  ml-debug:  sudo python3 /opt/mdt-tools/ml-debug/ml_debug.py"
echo
if (( NEED_REBOOT )); then
    warn "boot config changed -- REBOOT REQUIRED before services will work"
    echo "       sudo reboot"
fi
