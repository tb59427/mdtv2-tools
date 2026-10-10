"""Music recognition: what's playing, via Shazam -- on the turntable, and
on other sources the ML bus carries (core/ml_listen.py).

Runs inside audio_tap.py's worker process (never in the audio path) when
[turntable] recognize = true or [ml_listen] is enabled. The Shazam call itself -- fingerprinting is
CPU-heavy and holds the GIL -- runs in a helper process (this file with
--serve), so the worker keeps reading audio meanwhile. Needs shazamio -- an
unofficial Shazam client that may break whenever Shazam changes its API --
installed in its own venv by install.sh.

Policy, per track:

  * A track starts when the tap reports one: playback start, or music
    resuming after a silent gap (between tracks, record change).
  * First attempt once `snippet_s` seconds of the new track are buffered.
  * A result is shown only after two consecutive attempts agree
    (`confirm_s` apart). Rare tracks sometimes produce a one-off false
    match; requiring agreement filters those out.
  * No match: retry with backoff (20 s, 30 s, then every 60 s).
  * Confirmed: re-check every `verify_s`, in case the track changed without
    a gap. Two misses in a row, or two agreeing different results, replace
    or clear what's shown.

Results go out on redis `link:phono:recognized` (the bus listener uses
`link:ml:recognized`; pub/sub only -- nothing is stored, nothing touches
the SD card):

    {"state": "confirmed", "title": ..., "artist": ..., "album": ..., "cover_url": ...}
    {"state": "cleared"}
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import select
import struct
import subprocess
import sys
import threading
import time
import wave
from typing import Callable, Optional

RECOGNIZED_CHAN = "link:phono:recognized"

# Edition suffixes Shazam often reports instead of the original album:
# "Bella Donna (2016 Remaster)" -> "Bella Donna".
_EDITION_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(remaster(ed)?|deluxe|edition|expanded|anniversary"
    r"|bonus|version)\b[^\)\]]*[\)\]]\s*$", re.IGNORECASE)

_RETRY_S = (20.0, 30.0, 60.0)


def clean_album(album: str) -> str:
    prev = None
    while album and album != prev:              # "(Deluxe) [Remaster]"
        prev, album = album, _EDITION_RE.sub("", album)
    return album.strip()


def describe(result: dict) -> Optional[dict]:
    """shazamio response -> {title, artist, album, cover_url, isrc}, or None."""
    t = result.get("track")
    if not t or not t.get("title"):
        return None
    album = ""
    for sec in t.get("sections") or []:
        for m in sec.get("metadata") or []:
            if m.get("title") == "Album":
                album = m.get("text", "")
    return {
        "title": t.get("title", ""),
        "artist": t.get("subtitle", ""),
        "album": clean_album(album),
        "cover_url": (t.get("images") or {}).get("coverart", ""),
        "isrc": t.get("isrc", "") or "",
    }


def to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class Recognizer:
    """Owns the per-track policy. `snapshot(seconds)` returns the newest
    16-bit mono PCM; `publish(dict)` sends a result; `recognize(wav_bytes)`
    returns the raw Shazam response (injectable for tests)."""

    def __init__(self, *, rate: int,
                 snapshot: Callable[[float], bytes],
                 publish: Callable[[dict], None],
                 recognize: Optional[Callable] = None,
                 log: Callable[[str], None] = print,
                 snippet_s: float = 10.0, confirm_s: float = 10.0,
                 verify_s: float = 90.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = rate
        self.snapshot = snapshot
        self.publish = publish
        self.log = log
        self.snippet_s = snippet_s
        self.confirm_s = confirm_s
        self.verify_s = verify_s
        self.clock = clock
        self._recognize = recognize
        self._helper: Optional[subprocess.Popen] = None
        self._clear_pending = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._reset(self.clock())

    # ---- events from the tap (any thread) -----------------------------------

    def new_track(self) -> None:
        """Called from the audio-reading loop: only flips state; the
        publish happens on the recognition thread (no network here)."""
        with self._lock:
            if self._shown is not None:
                self._clear_pending = True
            self._reset(self.clock())
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._helper is not None:
            self._helper.kill()

    # ---- policy (pure, called from the worker thread) -----------------------

    def _reset(self, now: float) -> None:
        self._track_start = now
        self._next_at = now + self.snippet_s
        self._candidate: Optional[dict] = None
        self._shown: Optional[dict] = None
        self._misses = 0
        self._retry = 0

    @staticmethod
    def _key(info: Optional[dict]):
        return (info["artist"].lower(), info["title"].lower()) if info else None

    def on_result(self, info: Optional[dict], now: float) -> Optional[dict]:
        """Feed one attempt's result; returns what to publish (or None) and
        schedules the next attempt."""
        out = None
        with self._lock:
            key = self._key(info)
            if info is None:
                self._candidate = None
                if self._shown is not None:
                    self._misses += 1
                    if self._misses >= 2:
                        self.log("[recognize] lost the track -- clearing")
                        self._shown = None
                        out = {"state": "cleared"}
                    self._next_at = now + self.confirm_s
                else:
                    self._next_at = now + _RETRY_S[min(self._retry, len(_RETRY_S) - 1)]
                    self._retry += 1
            elif self._shown is not None and key == self._key(self._shown):
                self._misses = 0
                self._candidate = None
                self._next_at = now + self.verify_s
            elif key == self._key(self._candidate):
                self._shown, self._candidate = info, None
                self._misses = self._retry = 0
                self.log(f"[recognize] {info['artist']} - {info['title']} "
                         f"({info['album'] or '?'})")
                out = {"state": "confirmed", **info}
                self._next_at = now + self.verify_s
            else:
                self._candidate = info
                self._next_at = now + self.confirm_s
        return out

    # ---- worker thread ------------------------------------------------------

    def due_in(self, now: float) -> float:
        with self._lock:
            return self._next_at - now

    def attempt(self, now: float) -> None:
        """One recognition on the newest snippet_s seconds of this track."""
        pcm = self.snapshot(self.snippet_s)
        if not pcm:
            with self._lock:
                self._next_at = now + 2.0             # buffer not full yet
            return
        try:
            result = self._run(to_wav(pcm, self.rate))
        except Exception as e:                        # network, API change
            self.log(f"[recognize] request failed: {e}")
            with self._lock:
                self._next_at = now + 60.0
            return
        out = self.on_result(describe(result), self.clock())
        if out is not None:
            self.publish(out)

    def _run(self, wav: bytes) -> dict:
        if self._recognize is not None:
            return self._recognize(wav)
        return self._ask_helper(wav)

    def _ask_helper(self, wav: bytes, timeout: float = 30.0) -> dict:
        """One request to the --serve helper, (re)started as needed."""
        h = self._helper
        if h is None or h.poll() is not None:
            h = self._helper = subprocess.Popen(
                [sys.executable, "-u", __file__, "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            h.stdin.write(struct.pack("<I", len(wav)) + wav)
            h.stdin.flush()
            if not select.select([h.stdout], [], [], timeout)[0]:
                raise TimeoutError(f"no answer in {timeout:g} s")
            msg = json.loads(h.stdout.readline() or b"{}")
        except Exception:
            h.kill()
            self._helper = None
            raise
        if not msg.get("ok"):
            raise RuntimeError(msg.get("error", "helper died"))
        return msg["result"]

    def flush_clear(self) -> None:
        with self._lock:
            pending, self._clear_pending = self._clear_pending, False
        if pending:
            self.publish({"state": "cleared"})

    def run(self) -> None:
        self.publish({"state": "cleared"})
        while not self._stop.is_set():
            self.flush_clear()
            wait = self.due_in(self.clock())
            if wait > 0:
                self._wake.clear()
                self._wake.wait(min(wait, 5.0))
                continue
            self.attempt(self.clock())


def redis_publisher(host: str, port: int, log: Callable[[str], None],
                    channel: str = RECOGNIZED_CHAN) -> Callable[[dict], None]:
    import redis
    r = redis.StrictRedis(host=host, port=port, db=0)

    def publish(msg: dict) -> None:
        try:
            r.publish(channel, json.dumps(msg, ensure_ascii=False))
        except redis.exceptions.RedisError as e:
            log(f"[recognize] redis publish failed: {e}")
    return publish


def serve() -> None:
    """Helper process: length-prefixed WAV on stdin -> one JSON line per
    request on stdout. Keeps the CPU-heavy fingerprinting out of the
    worker, which must keep reading audio."""
    from shazamio import Shazam                       # venv-only dependency
    shazam = Shazam()
    loop = asyncio.new_event_loop()
    inp = sys.stdin.buffer
    while True:
        hdr = inp.read(4)
        if len(hdr) < 4:
            return                                    # worker gone
        (n,) = struct.unpack("<I", hdr)
        wav = inp.read(n)
        try:
            res = loop.run_until_complete(shazam.recognize(wav))
            line = json.dumps({"ok": True, "result": res})
        except Exception as e:
            line = json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"})
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


if __name__ == "__main__" and sys.argv[1:] == ["--serve"]:
    serve()
