"""Original album for a recognized track, from MusicBrainz.

Shazam often names a compilation instead of the album a track comes from
("Peaceful Choral Music" for a track from "Officium"). MusicBrainz knows
every release a recording is on, typed (Album / Compilation / ...) and with
the track's position, so we can pick the original:

  * candidates: releases whose release group is an Album with no secondary
    type (no Compilation, Live, Soundtrack, ...);
  * a release where the track sits at the position the audio master reports
    (CD track number) wins -- that's very likely the disc in the player;
  * otherwise the earliest release.

The recording is looked up by ISRC when Shazam reports one, else by title
and (first) artist. The cover comes from the Cover Art Archive for the
release group, if it has one. Plain urllib, no extra dependencies; one
request per new track, with MusicBrainz's required User-Agent.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

_API = "https://musicbrainz.org/ws/2/recording/"
_CAA = "https://coverartarchive.org/release-group/{}/front-500"
USER_AGENT = "mdtv2-tools/1.0 (https://github.com/tb59427/mdtv2-tools)"
_MIN_SCORE = 90          # MusicBrainz search score (0..100) for a recording


def _get(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _lucene(s: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", s)


def _first_artist(artist: str) -> str:
    """"Jan Garbarek & Hilliard Ensemble" -> "Jan Garbarek"."""
    return re.split(r"\s+(?:&|and|feat\.?|ft\.?|with|x)\s+|,\s*", artist,
                    maxsplit=1, flags=re.IGNORECASE)[0].strip()


def query_for(title: str, artist: str, isrc: str = "") -> str:
    if isrc:
        return f"isrc:{_lucene(isrc)}"
    q = f'recording:"{_lucene(title)}"'
    if artist:
        q += f' AND artist:"{_lucene(_first_artist(artist))}"'
    return q


def pick_release(recordings: list[dict], track: Optional[int]) -> Optional[dict]:
    """The original album among the recordings' releases (pure, testable).
    Returns {"album", "release_group", "date"} or None."""
    best = None
    best_key = None
    for rec in recordings:
        if int(rec.get("score", 100)) < _MIN_SCORE:
            continue
        for rel in rec.get("releases") or []:
            rg = rel.get("release-group") or {}
            if rg.get("primary-type") != "Album" or rg.get("secondary-types"):
                continue
            numbers = {str(t.get("number", "")) for m in rel.get("media") or []
                       for t in m.get("track") or []}
            at_track = track is not None and str(track) in numbers
            date = rel.get("date") or "9999"
            official = rel.get("status", "Official") == "Official"
            key = (not at_track, not official, date)       # smallest wins
            if best_key is None or key < best_key:
                best_key = key
                best = {"album": rel.get("title", ""), "release_group": rg.get("id", ""),
                        "date": rel.get("date", "")}
    return best


def cover_url(release_group: str, timeout: float = 5.0) -> str:
    """Cover Art Archive front cover for a release group, or "" if none."""
    if not release_group:
        return ""
    url = _CAA.format(release_group)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return url                                  # 307 -> image: exists
    except (urllib.error.URLError, OSError):
        return ""


def original_album(title: str, artist: str, *, isrc: str = "",
                   track: Optional[int] = None, timeout: float = 5.0) -> Optional[dict]:
    """{"album", "cover_url", "date"} for the original album, or None (no
    match, network error). Never raises."""
    try:
        url = _API + "?" + urllib.parse.urlencode(
            {"query": query_for(title, artist, isrc), "fmt": "json", "limit": 10})
        data = _get(url, timeout)
        rel = pick_release(data.get("recordings") or [], track)
        if rel is None and isrc:                        # ISRC unknown to MusicBrainz
            url = _API + "?" + urllib.parse.urlencode(
                {"query": query_for(title, artist), "fmt": "json", "limit": 10})
            rel = pick_release(_get(url, timeout).get("recordings") or [], track)
        if rel is None:
            return None
        return {"album": rel["album"], "date": rel["date"],
                "cover_url": cover_url(rel["release_group"], timeout)}
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
