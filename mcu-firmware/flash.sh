#!/usr/bin/env bash
# Flash an ATtiny826 .hex via serialUPDI on the Pi.
#
# Drives UART.SEL high so /dev/serial0 is routed to the chip's UPDI pin
# for the duration of the session, then drops it back to LOW so the Pi
# UART is reconnected to the firmware comms (PB2/PB3) on exit.
#
# Usage:
#   sudo /opt/mdt-tools/mcu-firmware/flash.sh /opt/mdt-tools/mcu-firmware/firmware-vX.Y.Z.hex
#
# pymcuprog lives in a hidden venv at /opt/mdt-tools/.pymcuprog-venv/,
# set up by install.sh. The user never has to touch a venv.
set -euo pipefail

HEX="${1:-}"
shift || true
EXTRA_ARGS=("$@")

PYMCUPROG="${PYMCUPROG:-/opt/mdt-tools/.pymcuprog-venv/bin/pymcuprog}"
PORT="${PORT:-/dev/serial0}"
DEVICE="${DEVICE:-attiny826}"
UART_SEL_PIN="${UART_SEL_PIN:-4}"

# Stop the broker so it releases /dev/serial0 -- the UPDI session needs
# exclusive access. Restart at the end via the trap.
BROKER_WAS_ACTIVE=0

if [[ -z $HEX || ! -f $HEX ]]; then
    echo "usage: $0 <firmware.hex> [extra pymcuprog args]" >&2
    echo "" >&2
    echo "available firmware in this directory:" >&2
    ls -1 "$(dirname "$0")"/*.hex 2>/dev/null | sed 's|^|  |' >&2
    exit 2
fi

if [[ ! -x $PYMCUPROG ]]; then
    echo "pymcuprog not found at $PYMCUPROG" >&2
    echo "Re-run mdt-tools/install.sh to set it up." >&2
    exit 3
fi

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (try: sudo $0 $HEX)" >&2
    exit 1
fi

cleanup() {
    local rc=$?
    echo "[flash] UART.SEL -> LOW (RPi UART routed to ATtiny PB2/PB3)"
    pinctrl set "$UART_SEL_PIN" op dl 2>/dev/null || true
    if (( BROKER_WAS_ACTIVE )); then
        echo "[flash] restarting mdtv2-broker"
        systemctl start mdtv2-broker.service 2>/dev/null || true
    fi
    exit $rc
}
trap cleanup EXIT INT TERM

# Stop broker if running (it holds /dev/serial0 open).
if systemctl is-active --quiet mdtv2-broker.service 2>/dev/null; then
    BROKER_WAS_ACTIVE=1
    echo "[flash] stopping mdtv2-broker (will restart after flash)"
    systemctl stop mdtv2-broker.service
    sleep 0.5
fi

echo "[flash] UART.SEL -> HIGH (RPi UART routed to UPDI)"
pinctrl set "$UART_SEL_PIN" op dh
sleep 0.05

echo "[flash] ping $DEVICE"
"$PYMCUPROG" ping -d "$DEVICE" -t uart -u "$PORT"

echo "[flash] write + erase + verify: $HEX"
"$PYMCUPROG" write -d "$DEVICE" -t uart -u "$PORT" \
    -f "$HEX" --erase --verify "${EXTRA_ARGS[@]}"

echo "[flash] done"
