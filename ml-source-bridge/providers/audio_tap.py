#!/usr/bin/env python3
"""Pass-through audio tap: raw PCM on stdin -> the same bytes on stdout,
plus periodic WAV snippets of what just went through.

Sits in the turntable provider's ADC -> DAC loopback, but only when
`tap_dir` is set in [turntable] (off by default):

    arecord ... | audio_tap.py ... | aplay ...

Every byte is forwarded immediately and unchanged; the listener hears no
difference. A copy of the last `--snippet-s` seconds is kept in memory, and
every `--interval-s` seconds a background thread writes it to `--dir` as a
16-bit mono WAV (about 1.4 MB per 15 s); files beyond `--keep` are deleted.
Used to collect samples for music recognition -- nothing here talks to the
network.

SD-card wear: point `--dir` at a tmpfs (/tmp on Debian 13, /dev/shm). The
tap warns at startup if it isn't one.

Only one process can open the ADC, which is why this taps the existing
pipeline instead of recording separately. Failures writing snippets are
logged to stderr and never stop the audio.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import threading
import time
import wave
from pathlib import Path

_CHUNK = 4096           # bytes per read; small = no added latency


def log(msg: str) -> None:
    print(f"[tap] {msg}", file=sys.stderr, flush=True)


def fs_type(path: Path) -> str:
    """Filesystem type of the mount holding `path` ("" if unknown)."""
    best, fstype = "", ""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                _dev, mnt, typ = line.split()[:3]
                if str(path).startswith(mnt) and len(mnt) > len(best):
                    best, fstype = mnt, typ
    except OSError:
        pass
    return fstype


class Ring:
    """The last `capacity` bytes that went through, frame-aligned."""

    def __init__(self, capacity: int, frame: int) -> None:
        self._cap = capacity - capacity % frame
        self._chunks: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._lock = threading.Lock()

    def add(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)
            self._size += len(data)
            while self._chunks and self._size - len(self._chunks[0]) >= self._cap:
                self._size -= len(self._chunks.popleft())

    def snapshot(self) -> bytes:
        """The newest `capacity` bytes, or b"" if not filled yet."""
        with self._lock:
            if self._size < self._cap:
                return b""
            data = b"".join(self._chunks)
        return data[len(data) - self._cap:]


def to_mono16(data: bytes, channels: int, width: int) -> bytes:
    """Interleaved little-endian PCM (16/32 bit) -> 16-bit mono. Plenty for
    recognition, and a quarter of the size of 32-bit stereo."""
    import numpy as np                      # only needed when writing
    a = np.frombuffer(data, dtype="<i2" if width == 2 else "<i4")
    a = a.reshape(-1, channels).astype(np.int64).mean(axis=1)
    if width == 4:
        a = a / 65536.0                     # keep the top 16 bits
    return np.clip(a, -32768, 32767).astype("<i2").tobytes()


def write_snippets(ring: Ring, out_dir: Path, interval_s: float, keep: int,
                   rate: int, channels: int, width: int,
                   stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        data = ring.snapshot()
        if not data:
            continue
        name = out_dir / time.strftime("phono-%Y%m%d-%H%M%S.wav")
        tmp = name.with_suffix(".tmp")
        try:
            pcm = to_mono16(data, channels, width)
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(pcm)
            os.replace(tmp, name)              # never expose half a file
            for f in sorted(out_dir.glob("phono-*.wav"))[:-keep]:
                f.unlink(missing_ok=True)
        except Exception as e:                 # never take the audio down
            log(f"writing {name.name} failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--bits", type=int, choices=(16, 32), default=32)
    ap.add_argument("--dir", required=True, help="where snippets go (tmpfs!)")
    ap.add_argument("--snippet-s", type=float, default=15.0)
    ap.add_argument("--interval-s", type=float, default=30.0)
    ap.add_argument("--keep", type=int, default=20,
                    help="max snippets kept in --dir")
    args = ap.parse_args()

    width = args.bits // 8
    frame = width * args.channels
    out_dir = Path(args.dir)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        typ = fs_type(out_dir.resolve())
        if typ != "tmpfs":
            log(f"WARNING: {out_dir} is on {typ or 'an unknown fs'}, not "
                f"tmpfs -- snippets wear the SD card. Use /tmp or /dev/shm.")
    except OSError as e:
        log(f"can't create {out_dir}: {e} -- passing audio through only")
        out_dir = None

    ring = Ring(int(args.rate * args.snippet_s) * frame, frame)
    stop = threading.Event()
    if out_dir is not None:
        threading.Thread(
            target=write_snippets, name="snippets", daemon=True,
            args=(ring, out_dir, args.interval_s, args.keep,
                  args.rate, args.channels, width, stop)).start()
        log(f"snippets of {args.snippet_s:g} s every {args.interval_s:g} s "
            f"-> {out_dir} (keeping {args.keep})")

    fin, fout = sys.stdin.fileno(), sys.stdout.fileno()
    try:
        while True:
            data = os.read(fin, _CHUNK)
            if not data:
                break
            view = memoryview(data)
            while view:                         # forward first, always
                n = os.write(fout, view)
                view = view[n:]
            ring.add(data)
    except BrokenPipeError:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
