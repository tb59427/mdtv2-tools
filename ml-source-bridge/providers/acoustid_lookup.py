"""AcoustID: identify a track from its first ~2 minutes, as a fallback when
Shazam doesn't know it.

AcoustID (acoustid.org) is MusicBrainz's open fingerprint database, filled
by people tagging their files; older CDs in particular are well covered.
Its fingerprints (Chromaprint, `fpcalc`) cover the start of a track, so we
need audio from the track's beginning -- the tap keeps it (`Head`).

The catch: a lookup also wants the track's total duration, and only
matches within about +-7 s of it (measured: a 290 s track was found with
283..297, not with 278 or 302). While the track plays we don't know it, so
we scan durations in 14 s steps -- likely lengths first -- and stop at the
first hit. At most ~45 requests, ~15 s at AcoustID's limit of 3 per second,
once per track that Shazam doesn't know.

Needs `fpcalc` (Debian package libchromaprint-tools) and an AcoustID API key
(free, acoustid.org -> "Register application"). Standard library only.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

API = "https://api.acoustid.org/v2/lookup"
HEAD_S = 120.0               # fpcalc fingerprints the first 120 s by default
MIN_SCORE = 0.5
_STEP_S = 14                 # +-7 s matching window per request
_MIN_INTERVAL_S = 0.34       # AcoustID: at most 3 requests per second


def scan_durations() -> list[int]:
    """Durations to try, most likely track lengths first (3..8 min, then
    longer, then shorter)."""
    likely = list(range(187, 481, _STEP_S))
    longer = list(range(481, 961, _STEP_S))
    shorter = list(range(173, int(HEAD_S), -_STEP_S))
    return likely + longer + shorter


def fingerprint(pcm: bytes, rate: int, timeout: float = 30.0) -> str:
    """16-bit mono PCM -> Chromaprint fingerprint (fpcalc via stdin)."""
    out = subprocess.run(
        ["fpcalc", "-format", "s16le", "-rate", str(rate), "-channels", "1",
         "-length", str(int(HEAD_S)), "-json", "-"],
        input=pcm, capture_output=True, timeout=timeout, check=True)
    return json.loads(out.stdout)["fingerprint"]


def best_match(data: dict) -> Optional[dict]:
    """AcoustID response -> {title, artist, recording_mbid} of the best
    result with a recording, or None (pure, testable)."""
    if data.get("status") != "ok":
        return None
    for res in sorted(data.get("results") or [], key=lambda r: -r.get("score", 0)):
        if res.get("score", 0) < MIN_SCORE:
            break
        for rec in res.get("recordings") or []:
            if rec.get("title"):
                artist = "".join(a.get("name", "") + (a.get("joinphrase") or "")
                                 for a in rec.get("artists") or []).strip()
                return {"title": rec["title"], "artist": artist,
                        "recording_mbid": rec.get("id", "")}
    return None


def identify(pcm: bytes, rate: int, key: str,
             log: Callable[[str], None] = print,
             timeout: float = 10.0) -> Optional[dict]:
    """Fingerprint the track head and scan AcoustID. Returns
    {title, artist, album, cover_url, isrc, recording_mbid} (album and cover
    empty -- the bridge looks those up on MusicBrainz) or None. Never raises."""
    try:
        fp = fingerprint(pcm, rate)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError) as e:
        log(f"[acoustid] fpcalc failed: {e}")
        return None
    last = 0.0
    for n, dur in enumerate(scan_durations(), 1):
        wait = _MIN_INTERVAL_S - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        query = urllib.parse.urlencode({"client": key, "duration": dur,
                                        "fingerprint": fp, "meta": "recordings"})
        try:
            with urllib.request.urlopen(f"{API}?{query}", timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"[acoustid] request failed: {e}")
            return None
        if data.get("status") != "ok":
            log(f"[acoustid] error: {data.get('error', data)}")
            return None
        hit = best_match(data)
        if hit:
            log(f"[acoustid] {hit['artist']} - {hit['title']} "
                f"(duration ~{dur} s, request {n})")
            return {**hit, "album": "", "cover_url": "", "isrc": ""}
    log("[acoustid] no match")
    return None
