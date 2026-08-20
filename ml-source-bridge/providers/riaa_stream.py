#!/usr/bin/env python3
"""Streaming RIAA de-emphasis filter: raw PCM on stdin -> raw PCM on stdout.

Sits in the middle of the turntable provider's ADC -> DAC loopback when
`riaa = true`, i.e.

    arecord ... | riaa_stream.py | aplay ...

Same curve as dl-scripts/dl80-turntable/capture-phono-riaa.sh (4th-order
Butterworth high-pass, then the standard 3180/318/75 us RIAA playback
curve, normalized to 0 dB at 1 kHz) -- but block-wise with persistent
filter state so it can run forever on a live stream.

DIFFERENCE FROM THE CAPTURE SCRIPT: that script peak-normalizes the
finished file to -1 dBFS. A live stream has no "finished file" to measure,
so instead a fixed makeup gain (--gain-db) is applied and the output is
hard-clipped to full scale. Set the gain to taste; too much just clips.

DEV / EXPERIMENTAL ONLY -- POOR SOUND QUALITY. Same caveat as the capture
script: this is for characterising the ADC path and playing with curves,
not for listening to records. A ~4 mV cartridge into a 3.3 V sigma-delta
ADC plus ~36 dB of make-up gain is thin, noisy and short on headroom. For
a turntable with no built-in RIAA preamp, use an EXTERNAL phono preamp
into a line input and leave this filter out of the chain.

Requires numpy + scipy (apt: python3-numpy python3-scipy).
"""
from __future__ import annotations

import argparse
import sys

DTYPE = {16: "<i2", 32: "<i4"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--bits", type=int, default=32, choices=(16, 32),
                    help="sample width; must match arecord/aplay format")
    ap.add_argument("--hp", type=float, default=30.0,
                    help="subsonic high-pass corner in Hz (0 disables)")
    ap.add_argument("--gain-db", type=float, default=0.0,
                    help="fixed makeup gain applied after the curve")
    ap.add_argument("--block", type=int, default=2048,
                    help="frames per processing block (latency vs syscalls)")
    args = ap.parse_args()

    try:
        import numpy as np
        import scipy.signal as sig
    except ImportError as e:                       # be explicit, not cryptic
        print(f"riaa_stream: needs numpy+scipy ({e}); "
              f"install python3-numpy python3-scipy, or set riaa = false",
              file=sys.stderr)
        return 2

    ch = args.channels
    dt = np.dtype(DTYPE[args.bits])
    full = float(2 ** (args.bits - 1))

    # Standard RIAA playback curve, bilinear-transformed at our rate and
    # normalized so the IIR is 0 dB at 1 kHz (same math as the script).
    T1, T2, T3 = 3180e-6, 318e-6, 75e-6
    num = np.poly1d([T2, 1.0])
    den = np.poly1d([T1, 1.0]) * np.poly1d([T3, 1.0])
    b, a = sig.bilinear(num.c, den.c, args.rate)
    _, H1k = sig.freqz(b, a, worN=[2 * np.pi * 1000 / args.rate])
    b = b / abs(H1k[0])

    # Persistent per-channel filter state, so blocks join seamlessly.
    zi_riaa = [sig.lfilter_zi(b, a) * 0.0 for _ in range(ch)]
    sos_hp = None
    zi_hp = None
    if args.hp > 0:
        sos_hp = sig.butter(4, args.hp, btype="highpass",
                            fs=args.rate, output="sos")
        zi_hp = [np.zeros((sos_hp.shape[0], 2)) for _ in range(ch)]

    gain = 10.0 ** (args.gain_db / 20.0)
    nbytes = args.block * ch * dt.itemsize
    rd, wr = sys.stdin.buffer, sys.stdout.buffer

    while True:
        buf = rd.read(nbytes)
        if not buf:
            break                                   # upstream closed
        # A short read at the tail is fine; just process what arrived.
        usable = (len(buf) // (ch * dt.itemsize)) * ch * dt.itemsize
        if usable == 0:
            break
        x = (np.frombuffer(buf[:usable], dtype=dt)
             .reshape(-1, ch).astype(np.float64) / full)

        out = np.empty_like(x)
        for c in range(ch):
            v = x[:, c]
            if sos_hp is not None:
                v, zi_hp[c] = sig.sosfilt(sos_hp, v, zi=zi_hp[c])
            v, zi_riaa[c] = sig.lfilter(b, a, v, zi=zi_riaa[c])
            out[:, c] = v * gain

        # Clip before quantizing -- without this, overs wrap around into
        # loud garbage instead of just distorting.
        np.clip(out, -1.0, 1.0 - 1.0 / full, out=out)
        wr.write((out * full).astype(dt).tobytes())
        wr.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
