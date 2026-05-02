#!/usr/bin/env bash
# capture-cd.sh
#
# Manual test script. DL'86 music system -> CD source on -> ADC AIN3 ->
# 60 s WAV -> FLAC -> standby.
#
# Self-contained: no helpers, no install. Just sudo this and you get a
# .flac of one minute of CD audio. Requires the broker running so the
# DL'86 bytes reach the music system.
#
# Wiring assumed:
#   CD player line-out (or BM tape-record-out for the CD source) wired
#   to the HAT's VIN3 single-ended (AIN3). Phono / VIN4 stays free for
#   the turntable.
#
# Usage:
#   sudo ./capture-cd.sh [output_basename]
#
# Env overrides:
#   DUR=60                          recording length, seconds
#   SPINUP_S=5                      wait between CD-on and arecord
#                                   (CD switches faster than a turntable
#                                   spins up; 5 s is plenty for the
#                                   source-relay + first-track read)
#   AUDIO_DEV=hw:sndrpihifiberry,0  ALSA capture device
#   I2C_BUS=1, ADDR=0x4a            PCM1862 I²C location
#   SKIP_STANDBY=0                  set to 1 to leave the system on
#                                   after recording (default: send
#                                   standby = Beo4 0x0C)
#
# Apt deps (one-time): redis-tools alsa-utils i2c-tools ffmpeg
set -euo pipefail

DUR="${DUR:-60}"
SPINUP_S="${SPINUP_S:-5}"
AUDIO_DEV="${AUDIO_DEV:-hw:sndrpihifiberry,0}"
I2C_BUS="${I2C_BUS:-1}"
ADDR="${ADDR:-0x4a}"
SKIP_STANDBY="${SKIP_STANDBY:-0}"
PREFIX="${1:-cd-$(date +%Y%m%d-%H%M%S)}"

WORK="$(mktemp -d -t cd-cap.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

for cmd in redis-cli arecord ffmpeg i2cset i2cget; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "missing: $cmd" >&2; exit 1; }
done

# 1. Music system: switch source to CD (Beo4 key 0x92 in our 17-bit frame).
echo "[cd] DL'86 CD source on  (17:00C920)"
redis-cli PUBLISH link:dl86:transmit 17:00C920 >/dev/null

# 2. Wait for source-switch + first-track ready.
echo "[cd] sleeping ${SPINUP_S}s for CD source switch + ready"
sleep "$SPINUP_S"

# 3. PCM1862: VIN3 single-ended (AIN3), 0 dB PGA on both channels.
#    CD line-out is ~2 Vrms (~+6 dBV) -- 0 dB through the chip's
#    ~2.1 Vrms full-scale puts peaks just below clipping. No PGA boost
#    needed (and would clip if applied).
echo "[cd] PCM1862 -> VIN3, 0 dB PGA"
i2cset -f -y "$I2C_BUS" "$ADDR" 0x06 0x44 b   # ADC1L = VIN3 single-ended
i2cset -f -y "$I2C_BUS" "$ADDR" 0x07 0x44 b   # ADC1R = VIN3 single-ended
i2cset -f -y "$I2C_BUS" "$ADDR" 0x01 0x00 b   # ADC1L PGA = 0 dB
i2cset -f -y "$I2C_BUS" "$ADDR" 0x02 0x00 b   # ADC1R PGA = 0 dB

# 4. Record.
echo "[cd] recording ${DUR}s"
arecord -q -D "$AUDIO_DEV" -f S32_LE -r 48000 -c 2 -d "$DUR" "$WORK/raw.wav"

# 5. Standby (unless caller said skip).
if [[ "$SKIP_STANDBY" != "1" ]]; then
    echo "[cd] DL'86 standby     (17:008600)"
    redis-cli PUBLISH link:dl86:transmit 17:008600 >/dev/null
fi

# 6. Encode to FLAC.
out="${PREFIX}.flac"
echo "[cd] encoding -> $out"
ffmpeg -hide_banner -loglevel error -y \
    -i "$WORK/raw.wav" \
    -c:a flac -compression_level 5 -sample_fmt s32 \
    "$out"

ls -lh "$out"
echo "[cd] done"
