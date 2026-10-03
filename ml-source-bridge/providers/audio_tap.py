#!/usr/bin/env python3
"""Pass-through audio tap: raw PCM on stdin -> the same bytes on stdout,
with music recognition and/or test snippets on the side.

Sits in the turntable provider's ADC -> DAC loopback, but only when
[turntable] recognize = true or tap_dir is set (both off by default):

    arecord ... | audio_tap.py ... | aplay ...

Two processes, so nothing can hold up the audio:

  * The main process only forwards: every byte goes to stdout immediately
    and unchanged. It also hands a copy to the worker over a non-blocking
    pipe; if the worker falls behind, copies are dropped, never the audio.
  * The worker keeps the last seconds as 16-bit mono in memory, detects
    track boundaries (silent gaps), and
      - with --recognize: identifies the music (phono_recognize.py) and
        publishes the result on redis -- nothing is written to disk;
      - with --dir: writes a WAV snippet every --interval-s (for tests;
        point it at a tmpfs, /tmp or /dev/shm, not the SD card).

Only one process can open the ADC, which is why this taps the existing
pipeline instead of recording separately.
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

_CHUNK = 4096           # bytes per read; <= PIPE_BUF so tee writes are atomic


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


# ---- worker ---------------------------------------------------------------

class Ring:
    """The newest `capacity_s` seconds of 16-bit mono PCM."""

    def __init__(self, rate: int, capacity_s: float) -> None:
        self.rate = rate
        self._cap = int(rate * capacity_s) * 2
        self._chunks: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._lock = threading.Lock()

    def add(self, pcm: bytes) -> None:
        with self._lock:
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._chunks and self._size - len(self._chunks[0]) >= self._cap:
                self._size -= len(self._chunks.popleft())

    def snapshot(self, seconds: float) -> bytes:
        """The newest `seconds` of audio, or b"" if not that much yet."""
        want = int(self.rate * seconds) * 2
        with self._lock:
            if self._size < want:
                return b""
            data = b"".join(self._chunks)
        return data[len(data) - want:]


class GapDetector:
    """Calls `on_track` when music resumes after >= min_gap_s below
    silence_db (between tracks, or a record change). Works on 100 ms
    blocks; measured on a Beogram: music never below about -47 dBFS,
    the groove between tracks about -63 dBFS for several seconds."""

    def __init__(self, rate: int, on_track, silence_db: float = -50.0,
                 min_gap_s: float = 1.5, resume_s: float = 0.3) -> None:
        import numpy as np
        self._np = np
        self._block = rate // 10
        self._buf = np.zeros(0, dtype=np.int16)
        self._on_track = on_track
        self._thresh = 32768.0 * 10 ** (silence_db / 20)
        self._gap_blocks = round(min_gap_s * 10)
        self._resume_blocks = max(1, round(resume_s * 10))
        self._quiet = self._loud = 0
        self.in_gap = False

    def feed(self, samples) -> None:
        np = self._np
        buf = np.concatenate((self._buf, samples))
        n = len(buf) // self._block
        for i in range(n):
            blk = buf[i * self._block:(i + 1) * self._block].astype(np.float32)
            if np.sqrt(np.mean(blk * blk)) < self._thresh:
                self._quiet += 1
                self._loud = 0
                if self._quiet >= self._gap_blocks and not self.in_gap:
                    self.in_gap = True
                    log("gap")
            else:
                self._loud += 1
                self._quiet = 0
                if self.in_gap and self._loud >= self._resume_blocks:
                    self.in_gap = False
                    self._on_track()
        self._buf = buf[n * self._block:]


def write_snippets(ring: Ring, out_dir: Path, snippet_s: float,
                   interval_s: float, keep: int, stop: threading.Event) -> None:
    while not stop.wait(interval_s):
        pcm = ring.snapshot(snippet_s)
        if not pcm:
            continue
        name = out_dir / time.strftime("phono-%Y%m%d-%H%M%S.wav")
        tmp = name.with_suffix(".tmp")
        try:
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(ring.rate)
                w.writeframes(pcm)
            os.replace(tmp, name)              # never expose half a file
            for f in sorted(out_dir.glob("phono-*.wav"))[:-keep]:
                f.unlink(missing_ok=True)
        except Exception as e:
            log(f"writing {name.name} failed: {e}")


def run_worker(fd: int, args: argparse.Namespace) -> None:
    import numpy as np
    width = args.bits // 8
    frame = width * args.channels
    dtype = "<i2" if width == 2 else "<i4"
    stop = threading.Event()

    ring = Ring(args.rate, max(args.snippet_s, args.recognize_s) + 2)

    recognizer = None
    if args.recognize:
        try:
            import phono_recognize as pr
            recognizer = pr.Recognizer(
                rate=args.rate, snapshot=ring.snapshot,
                publish=pr.redis_publisher(args.redis_host, args.redis_port, log),
                log=log, snippet_s=args.recognize_s)
            threading.Thread(target=recognizer.run, name="recognize",
                             daemon=True).start()
            log(f"music recognition on ({args.recognize_s:g} s snippets)")
        except Exception as e:                  # shazamio missing, ...
            log(f"music recognition unavailable: {e}")
            recognizer = None

    def on_track() -> None:
        log("new track")
        if recognizer is not None:
            recognizer.new_track()
    gaps = GapDetector(args.rate, on_track, silence_db=args.silence_db)

    if args.dir:
        out_dir = Path(args.dir)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            typ = fs_type(out_dir.resolve())
            if typ != "tmpfs":
                log(f"WARNING: {out_dir} is on {typ or 'an unknown fs'}, not "
                    f"tmpfs -- snippets wear the SD card. Use /tmp or /dev/shm.")
            threading.Thread(
                target=write_snippets, name="snippets", daemon=True,
                args=(ring, out_dir, args.snippet_s, args.interval_s,
                      args.keep, stop)).start()
            log(f"snippets of {args.snippet_s:g} s every {args.interval_s:g} s "
                f"-> {out_dir} (keeping {args.keep})")
        except OSError as e:
            log(f"can't use {out_dir}: {e} -- no snippets")

    rest = b""
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                break                           # main process gone
            data = rest + data
            cut = len(data) - len(data) % frame
            data, rest = data[:cut], data[cut:]
            if not data:
                continue
            a = np.frombuffer(data, dtype=dtype).reshape(-1, args.channels)
            mono = a.astype(np.int64).mean(axis=1)
            if width == 4:
                mono = mono / 65536.0           # keep the top 16 bits
            pcm = np.clip(mono, -32768, 32767).astype("<i2")
            ring.add(pcm.tobytes())
            gaps.feed(pcm)
    finally:
        stop.set()
        if recognizer is not None:
            recognizer.stop()


# ---- main process ---------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--bits", type=int, choices=(16, 32), default=32)
    ap.add_argument("--recognize", action="store_true",
                    help="identify the music, publish on redis")
    ap.add_argument("--recognize-s", type=float, default=10.0,
                    help="snippet length per recognition attempt")
    ap.add_argument("--silence-db", type=float, default=-50.0,
                    help="below this a stretch counts as a gap between tracks")
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--dir", default="", help="write test snippets here (tmpfs!)")
    ap.add_argument("--snippet-s", type=float, default=15.0)
    ap.add_argument("--interval-s", type=float, default=30.0)
    ap.add_argument("--keep", type=int, default=20,
                    help="max snippets kept in --dir")
    args = ap.parse_args()

    tee = None
    if args.recognize or args.dir:
        r, w = os.pipe()
        if os.fork() == 0:                      # worker
            os.close(w)
            # Let go of the pipeline's stdin/stdout, or arecord/aplay never
            # see EOF when the main process exits.
            null = os.open(os.devnull, os.O_RDWR)
            os.dup2(null, 0)
            os.dup2(null, 1)
            try:
                run_worker(r, args)
            except Exception as e:
                log(f"worker died: {e}")
            os._exit(0)
        os.close(r)
        os.set_blocking(w, False)
        try:                                    # Linux: ~2.7 s of S32 stereo
            import fcntl
            fcntl.fcntl(w, 1031, 1 << 20)       # F_SETPIPE_SZ
        except (ImportError, OSError):
            pass
        tee = w

    fin, fout = sys.stdin.fileno(), sys.stdout.fileno()
    dropped = reported = 0
    last_report = time.monotonic()
    try:
        while True:
            data = os.read(fin, _CHUNK)
            if not data:
                break
            view = memoryview(data)
            while view:                         # forward first, always
                n = os.write(fout, view)
                view = view[n:]
            if tee is not None:
                try:
                    os.write(tee, data)         # atomic: len <= PIPE_BUF
                except BlockingIOError:
                    dropped += 1                # worker behind: skip a copy
                    now = time.monotonic()
                    if now - last_report >= 10.0:
                        log(f"worker behind: {dropped - reported} chunks "
                            f"not analysed")
                        reported, last_report = dropped, now
                except OSError:
                    os.close(tee)               # worker gone: audio goes on
                    tee = None
                    log("worker gone -- passing audio through only")
    except BrokenPipeError:
        pass
    if dropped > reported:
        log(f"worker behind: {dropped - reported} chunks not analysed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
