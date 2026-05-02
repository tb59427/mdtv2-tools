#!/usr/bin/env bash
# capture-phono-raw.sh
#
# Manual test script. Beogram -> 0 dB ADC -> 60 s WAV -> FLAC.
# No RIAA, no normalization. Honest flat measurement.
#
# Self-contained: no helpers, no install. Just sudo this and you get a
# .flac of one minute. Requires the broker running so the DL'80 byte
# reaches the turntable.
#
# Usage:
#   sudo ./capture-phono-raw.sh [output_basename]
#
# Env overrides:
#   DUR=60                          recording length, seconds
#   SPINUP_S=10                     wait between BG.Play and arecord
#   AUDIO_DEV=hw:sndrpihifiberry,0  ALSA capture device
#   I2C_BUS=1, ADDR=0x4a            PCM1862 I²C location
#
# Apt deps (one-time): redis-tools alsa-utils i2c-tools ffmpeg
set -euo pipefail

DUR="${DUR:-60}"
SPINUP_S="${SPINUP_S:-10}"
AUDIO_DEV="${AUDIO_DEV:-hw:sndrpihifiberry,0}"
I2C_BUS="${I2C_BUS:-1}"
ADDR="${ADDR:-0x4a}"
PREFIX="${1:-phono-raw-$(date +%Y%m%d-%H%M%S)}"

WORK="$(mktemp -d -t phono-raw.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

for cmd in redis-cli arecord ffmpeg i2cset i2cget; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "missing: $cmd" >&2; exit 1; }
done

# 1. Beogram BG.Play
echo "[raw] Beogram BG.Play"
redis-cli PUBLISH link:dl80:transmit a9 >/dev/null

# 2. spin-up
echo "[raw] sleeping ${SPINUP_S}s for spin-up"
sleep "$SPINUP_S"

# 3. PCM1862: VIN4 single-ended, 0 dB PGA on both channels.
echo "[raw] PCM1862 -> VIN4, 0 dB PGA"
i2cset -f -y "$I2C_BUS" "$ADDR" 0x06 0x48 b   # ADC1L = VIN4
i2cset -f -y "$I2C_BUS" "$ADDR" 0x07 0x48 b   # ADC1R = VIN4
i2cset -f -y "$I2C_BUS" "$ADDR" 0x01 0x00 b   # ADC1L PGA = 0 dB
i2cset -f -y "$I2C_BUS" "$ADDR" 0x02 0x00 b   # ADC1R PGA = 0 dB

# 4. record
echo "[raw] recording ${DUR}s"
arecord -q -D "$AUDIO_DEV" -f S32_LE -r 48000 -c 2 -d "$DUR" "$WORK/raw.wav"

# 5. encode to FLAC, no processing
out="${PREFIX}.flac"
echo "[raw] encoding -> $out"
ffmpeg -hide_banner -loglevel error -y \
    -i "$WORK/raw.wav" \
    -c:a flac -compression_level 5 -sample_fmt s32 \
    "$out"

ls -lh "$out"
echo "[raw] done.  stop turntable: redis-cli PUBLISH link:dl80:transmit cb"
