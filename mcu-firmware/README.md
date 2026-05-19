# mcu-firmware (binary distribution)

This directory ships the **pre-built MDT MCU firmware binary** plus
`flash.sh`, the serialUPDI flasher.

The firmware **source** lives in a separate git repo. After building it
there (`make` in that repo's root), copy `build/firmware.hex` into here
as `firmware-vMAJOR.MINOR.PATCH.hex`.

## Flashing

`install.sh` sets up everything `flash.sh` needs (a hidden pymcuprog
venv at `/opt/mdt-tools/.pymcuprog-venv/`, the `pinctrl` package, etc.).
Then on the Pi:

```sh
sudo /opt/mdt-tools/mcu-firmware/flash.sh \
     /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex
```

The flasher:
1. Stops `mdtv2-broker` so it releases `/dev/serial0`.
2. Drives `UART.SEL` (GPIO4) high so the Pi UART is wired to the chip's
   UPDI pin instead of its firmware comms pins.
3. Runs `pymcuprog ping`, `write --erase --verify`.
4. Drops `UART.SEL` back to LOW and restarts the broker.

All in one shot. Re-runs are safe.

## Versions

| Version | Highlights |
|---|---|
| v1.5.5 | RX reliability hardening: smart MARK-mid-telegram, length-based EOL recovery, FERR/BUFOVF byte-discard. |
| v1.5.4 | `ML_MAX_TELEGRAM` 64 → 128 (long EXTENDED_SOURCE_INFO frames). |
| v1.5.3 | 9-bit framing on RX with correct DATAL-then-DATAH read order. |
| v1.5.0 | Removed RX address allowlist (slave-to-slave traffic now passes). |
