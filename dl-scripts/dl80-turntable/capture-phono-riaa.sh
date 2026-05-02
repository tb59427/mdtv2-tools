#!/usr/bin/env bash
# capture-phono-riaa.sh
#
# ##########################################################################
# # EXPERIMENTAL -- TEST PURPOSE ONLY                                       #
# #                                                                        #
# # This script captures the cartridge raw (no preamp) and applies the     #
# # RIAA curve in software. It exists so we can characterise the ADC path  #
# # and play with curves; it is NOT the recommended way to listen to a     #
# # record with this hardware.                                             #
# #                                                                        #
# # For normal listening use the turntable's BUILT-IN RIAA preamp (line-   #
# # level out -> any normal line input). A proper analogue phono stage     #
# # will out-perform a 3.3 V σ-Δ ADC fed by a 4 mV cartridge every time,   #
# # and you skip the +32 dB PGA shenanigans this script needs to keep the  #
# # signal above the chip's idle-noise floor.                              #
# ##########################################################################
#
# Beogram -> +32 dB ADC -> 60 s WAV -> RIAA hp30 -> FLAC.
#
# Self-contained: no helpers, no install. Just sudo this and you get a
# .flac of one minute of your record. Requires the broker running so the
# DL'80 byte reaches the turntable.
#
# Usage:
#   sudo ./capture-phono-riaa.sh [output_basename]
#
# Env overrides:
#   DUR=60                          recording length, seconds
#   SPINUP_S=10                     wait between BG.Play and arecord
#   AUDIO_DEV=hw:sndrpihifiberry,0  ALSA capture device
#   I2C_BUS=1, ADDR=0x4a            PCM1862 I²C location
#
# Apt deps (one-time): redis-tools alsa-utils i2c-tools ffmpeg
#                      python3-numpy python3-scipy python3-soundfile
set -euo pipefail

DUR="${DUR:-60}"
SPINUP_S="${SPINUP_S:-10}"
AUDIO_DEV="${AUDIO_DEV:-hw:sndrpihifiberry,0}"
I2C_BUS="${I2C_BUS:-1}"
ADDR="${ADDR:-0x4a}"
PREFIX="${1:-phono-$(date +%Y%m%d-%H%M%S)}"

WORK="$(mktemp -d -t phono-riaa.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

for cmd in redis-cli arecord ffmpeg python3 i2cset i2cget; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "missing: $cmd" >&2; exit 1; }
done

# 1. Beogram BG.Play
echo "[riaa] Beogram BG.Play"
redis-cli PUBLISH link:dl80:transmit a9 >/dev/null

# 2. spin-up
echo "[riaa] sleeping ${SPINUP_S}s for spin-up"
sleep "$SPINUP_S"

# 3. PCM1862: VIN4 single-ended, +32 dB PGA on both channels.
echo "[riaa] PCM1862 -> VIN4, +32 dB PGA"
i2cset -f -y "$I2C_BUS" "$ADDR" 0x06 0x48 b   # ADC1L = VIN4
i2cset -f -y "$I2C_BUS" "$ADDR" 0x07 0x48 b   # ADC1R = VIN4
i2cset -f -y "$I2C_BUS" "$ADDR" 0x01 0x48 b   # ADC1L PGA = +32 dB
i2cset -f -y "$I2C_BUS" "$ADDR" 0x02 0x48 b   # ADC1R PGA = +32 dB

# 4. record
echo "[riaa] recording ${DUR}s"
arecord -q -D "$AUDIO_DEV" -f S32_LE -r 48000 -c 2 -d "$DUR" "$WORK/raw.wav"

echo "[riaa] PCM1862 -> VIN4, 0 dB PGA"
i2cset -f -y "$I2C_BUS" "$ADDR" 0x01 0x0 b   # ADC1L PGA = 0 dB
i2cset -f -y "$I2C_BUS" "$ADDR" 0x02 0x0 b   # ADC1R PGA = 0 dB

echo "[riaa] Beogram BG.Stby"
redis-cli PUBLISH link:dl80:transmit cb >/dev/null

# 5. RIAA (hp30): 4th-order Butterworth HP at 30 Hz, then standard RIAA
#    (3180/318/75 µs), normalized so the IIR is 0 dB at 1 kHz, peak
#    pulled to -1 dBFS at the end.
echo "[riaa] applying RIAA (hp30)"
python3 - "$WORK/raw.wav" "$WORK/riaa.wav" <<'PY'
import sys
import numpy as np
import scipy.signal as sig
import soundfile as sf

src, dst = sys.argv[1], sys.argv[2]
data, fs = sf.read(src, dtype="int32")
x = data.astype(np.float64) / (2**31)

# 4th-order Butterworth HP at 30 Hz (subsonic / motor / warp rejection).
sos_hp = sig.butter(4, 30, btype="highpass", fs=fs, output="sos")

# Standard RIAA playback curve (T1=3180 µs, T2=318 µs, T3=75 µs),
# bilinear-transformed at the file's sample rate, normalized 0 dB @ 1 kHz.
T1, T2, T3 = 3180e-6, 318e-6, 75e-6
num = np.poly1d([T2, 1.0])
den = np.poly1d([T1, 1.0]) * np.poly1d([T3, 1.0])
b, a = sig.bilinear(num.c, den.c, fs)
_, H1k = sig.freqz(b, a, worN=[2 * np.pi * 1000 / fs])
b = b / abs(H1k[0])

def chan_filt(v):
    v = sig.sosfilt(sos_hp, v)
    return sig.lfilter(b, a, v)

y = np.column_stack([chan_filt(x[:, c]) for c in range(x.shape[1])]) \
    if x.ndim == 2 else chan_filt(x)

# peak-normalize to -1 dBFS
peak = float(np.max(np.abs(y))) or 1.0
y = y * (10 ** (-1.0 / 20.0) / peak)

sf.write(dst, (y * (2**31 - 1)).astype(np.int32), fs, subtype="PCM_32")
PY

# 6. encode to FLAC
out="${PREFIX}.flac"
echo "[riaa] encoding -> $out"
ffmpeg -hide_banner -loglevel error -y \
    -i "$WORK/riaa.wav" \
    -c:a flac -compression_level 5 -sample_fmt s32 \
    "$out"

ls -lh "$out"
echo "[riaa] done."
