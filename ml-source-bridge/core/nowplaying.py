"""Now-playing publisher -- what each of our ML sources is playing, on redis.

The bridge already polls every provider for state and metadata, but only
feeds the result to the B&O display. This module puts the same view on
redis so other processes (ha-notifier, dashboards, ...) can follow it
without talking D-Bus / MPD themselves.

Per source, on every change:

    HSET    state:nowplaying  <source_byte hex>  <json>    (current view)
    PUBLISH link:ml:nowplaying                   <json>    (change event)

Payload (one source):

    {"source": "N.MUSIC", "source_byte": "0x7a",
     "provider": "airplay", "display": "Apple Music",
     "state": "playing", "title": "...", "artist": "...", "album": "...",
     "art_url": "file:///tmp/shairport-sync/.cache/coverart/cover-….jpg"}

`state` is playing / paused / idle. When idle, provider / display /
title / artist / album / art_url are empty strings. `art_url` is what
the backend reports: a local file:// path or an http(s) URL --
ha-notifier turns local paths into something HA can fetch. The payload carries no
timestamp on purpose: identical state -> identical bytes, so consumers
can drop duplicates by comparing strings.

Runs independent of role (AM/SC) and of auto_wake.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

import redis

from core.bus import log
from providers.base import STATE_IDLE, SourceProvider
from providers.multi import MultiSourceProvider

NOWPLAYING_KEY  = "state:nowplaying"      # HGETALL -> {src_hex: json}
NOWPLAYING_CHAN = "link:ml:nowplaying"


def _field(src: int) -> str:
    return f"0x{src:02x}"


def _idle(provider: SourceProvider, source_display: str) -> dict[str, str]:
    return {
        "source": source_display,
        "source_byte": _field(provider.source_byte),
        "provider": "", "display": "", "state": STATE_IDLE,
        "title": "", "artist": "", "album": "", "art_url": "",
    }


def snapshot(provider: SourceProvider, source_display: str,
             displays: dict[str, str]) -> dict[str, str]:
    """Build the payload for one source. `displays` is the config's
    [provider_displays] table; a provider without an entry falls back
    to the source's display_name."""
    if isinstance(provider, MultiSourceProvider):
        sub: Optional[SourceProvider] = provider.current_sub()
    else:
        sub = provider

    blob = _idle(provider, source_display)
    if sub is None:
        return blob
    blob["state"] = sub.playback_state()
    if blob["state"] == STATE_IDLE:
        return blob

    blob["provider"] = sub.provider_name
    blob["display"] = displays.get(sub.provider_name, source_display)
    md = sub.metadata()
    if md is not None:
        blob["title"] = md.title or ""
        blob["artist"] = md.artist or ""
        blob["album"] = md.album or ""
    blob["art_url"] = sub.art_url() or ""
    return blob


def _write(r: redis.StrictRedis, src: int, blob: dict[str, str]) -> None:
    s = json.dumps(blob, ensure_ascii=False, sort_keys=True)
    r.hset(NOWPLAYING_KEY, _field(src), s)
    r.publish(NOWPLAYING_CHAN, s)


def write(r: redis.StrictRedis, src: int, blob: dict[str, str]) -> None:
    """Publish a view for a source no provider of ours owns (the bus
    listener's recognitions). Raises redis errors to the caller."""
    _write(r, src, blob)


def reset(r: redis.StrictRedis) -> None:
    """Drop the previous run's view (sources may have been removed from
    the config since)."""
    try:
        r.delete(NOWPLAYING_KEY)
    except redis.exceptions.RedisError as e:
        log(f"[nowplaying] redis reset failed: {e}", err=True)


def publisher_factory(r: redis.StrictRedis, provider: SourceProvider,
                      source_display: str,
                      displays: dict[str, str]) -> Callable[[], None]:
    """Periodic job for one source: snapshot, publish on change. The
    first call always publishes, so the hash is filled right after
    startup."""
    last: dict[str, Optional[dict]] = {"blob": None}

    def publish() -> None:
        blob = snapshot(provider, source_display, displays)
        if blob == last["blob"]:
            return
        try:
            _write(r, provider.source_byte, blob)
        except redis.exceptions.RedisError as e:
            # Leave `last` alone so the next poll retries.
            log(f"[nowplaying] redis publish failed: {e}", err=True)
            return
        last["blob"] = blob
        log(f"[nowplaying] {source_display}: {blob['state']} "
            f"{blob['provider'] or '-'} {blob['title']!r}")
    return publish


def publish_idle(r: redis.StrictRedis, provider: SourceProvider,
                 source_display: str) -> None:
    """Shutdown: the bridge is going away, so nothing reaches ML any
    more -- tell consumers each source is idle."""
    try:
        _write(r, provider.source_byte, _idle(provider, source_display))
    except redis.exceptions.RedisError as e:
        log(f"[nowplaying] redis publish failed: {e}", err=True)
