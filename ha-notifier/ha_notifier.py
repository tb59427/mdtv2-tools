#!/usr/bin/env python3
"""ha-notifier -- forward the bridge's now-playing view to a Home Assistant
webhook.

ml-source-bridge publishes, per ML source, which provider is playing and
what (see ml-source-bridge/README.md, "Now playing on redis"):

    HSET    state:nowplaying  <source_byte hex>  <json>
    PUBLISH link:ml:nowplaying                   <json>

This daemon subscribes to that channel and POSTs each change as JSON to
the configured HA webhook, so HA can show e.g. an AirPlay stream from an
iPhone that Music Assistant knows nothing about.

Behaviour:
  * Debounce per source: a change is sent once it has been stable for
    `debounce_ms`; quick successive changes collapse into the last one.
    Transitions away from "playing" wait `idle_debounce_ms` instead --
    shairport-sync briefly reports Paused while the ML system wakes up,
    and that blip shouldn't make the HA card flicker.
  * Duplicates are dropped: a payload identical to the last one HA
    accepted is not sent again -- except every `keepalive_s`, so HA
    catches up after a restart (trigger sensors only update on a POST).
  * HA unreachable: logged, never fatal; retried after `retry_s`.
  * On startup the current state (from state:nowplaying) is sent once.
  * Cover art: the bridge reports `art_url` as the backend gives it. A
    local file (shairport-sync's cover cache) is served over HTTP on
    `cover_port` and sent to HA as `cover_url`; an http(s) URL is passed
    through. Only files the bridge has reported are served.
  * Sources: `sources` lists display names or source bytes to forward
    (none = all). "ml_listen" stands for every source the bus listener
    reports (music recognition on CD, A.MEM, ...; "origin": "ml_listen").

Config: [ha_notifier] in /etc/ml-source-bridge.toml (see
ml-source-bridge/config.toml.example). While disabled it idles, waiting
for mdt-web's restart request (redis link:ctl:restart) after a config
change; on that request it exits with RESTART_EXIT so systemd
(Restart=on-failure) starts it again with the new config.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import mimetypes
import signal
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import redis

NOWPLAYING_KEY  = "state:nowplaying"
NOWPLAYING_CHAN = "link:ml:nowplaying"
CTL_RESTART     = "link:ctl:restart"
RESTART_EXIT    = 3          # non-zero on purpose: Restart=on-failure

DEFAULT_CONFIG = "/etc/ml-source-bridge.toml"


def log(msg: str) -> None:
    print(f"[ha-notifier] {msg}", flush=True)


# ---- config -----------------------------------------------------------------

@dataclass
class Config:
    url: str
    timeout_s: float = 3.0
    debounce_ms: int = 300
    idle_debounce_ms: int = 3000
    keepalive_s: float = 300.0          # 0 = never resend unchanged state
    retry_s: float = 30.0
    sources: Optional[set[str]] = None  # display names / "0x7a"; None = all
    cover_port: int = 8099              # 0 = don't serve local cover files
    cover_base_url: str = ""            # how the browser reaches us
    redis_host: str = "localhost"
    redis_port: int = 6379


def load_config(path: str) -> Optional[Config]:
    """Config, or None if [ha_notifier] is missing or disabled."""
    try:
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(f"config file not found: {path}")
    sec = cfg.get("ha_notifier") or {}
    if not sec.get("enabled", False):
        return None
    url = str(sec.get("url", "")).strip()
    if not url or "<" in url:
        raise SystemExit("[ha_notifier] enabled but url is not set")
    sources = sec.get("sources")
    cover_port = int(sec.get("cover_port", 8099))
    cover_base_url = str(sec.get("cover_base_url", "")).strip().rstrip("/")
    if cover_port and not cover_base_url:
        cover_base_url = f"http://{socket.gethostname()}.local:{cover_port}"
    return Config(
        url=url,
        timeout_s=float(sec.get("timeout_s", 3.0)),
        debounce_ms=int(sec.get("debounce_ms", 300)),
        idle_debounce_ms=int(sec.get("idle_debounce_ms", 3000)),
        keepalive_s=float(sec.get("keepalive_s", 300.0)),
        retry_s=float(sec.get("retry_s", 30.0)),
        sources={str(s).lower() for s in sources} if sources else None,
        cover_port=cover_port,
        cover_base_url=cover_base_url,
        redis_host=str(cfg.get("redis_host", "localhost")),
        redis_port=int(cfg.get("redis_port", 6379)),
    )


# ---- cover art --------------------------------------------------------------

class Covers:
    """Maps local cover files the bridge reported to short URL tokens and
    serves exactly those -- nothing else on the filesystem is reachable.
    Remembers the last few, so a track change doesn't break an image a
    browser is still fetching."""

    _KEEP = 16

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._paths: collections.OrderedDict[str, str] = collections.OrderedDict()
        self._lock = threading.Lock()

    def url_for(self, art_url: str) -> str:
        """cover_url for HA: http(s) passes through, file:// is mapped to
        our server (token = hash of the path, so a new cover gets a new
        URL and the browser refetches), anything else -> ""."""
        if art_url.startswith(("http://", "https://")):
            return art_url
        if not art_url.startswith("file://") or not self.base_url:
            return ""
        path = urllib.parse.unquote(urllib.parse.urlparse(art_url).path)
        token = hashlib.sha1(path.encode()).hexdigest()[:16]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "jpg"
        with self._lock:
            self._paths[token] = path
            self._paths.move_to_end(token)
            while len(self._paths) > self._KEEP:
                self._paths.popitem(last=False)
        return f"{self.base_url}/cover/{token}.{ext}"

    def path_for(self, token: str) -> Optional[str]:
        with self._lock:
            return self._paths.get(token)


def serve_covers(covers: Covers, port: int) -> None:
    """HTTP server for /cover/<token>.<ext> in a daemon thread."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            name = self.path.split("?", 1)[0]
            token = name.removeprefix("/cover/").split(".", 1)[0]
            path = covers.path_for(token) if name.startswith("/cover/") else None
            if path is None:
                self.send_error(404)
                return
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError as e:
                # Typically: shairport-sync already deleted it, or it's
                # not readable for our user.
                log(f"cover {path}: {e}")
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type",
                             mimetypes.guess_type(path)[0] or "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            pass

    srv = ThreadingHTTPServer(("", port), Handler)
    threading.Thread(target=srv.serve_forever, name="covers",
                     daemon=True).start()
    log(f"serving cover art on port {port} as {covers.base_url}/cover/…")


# ---- per-source send state --------------------------------------------------

@dataclass
class Slot:
    pending: Optional[str] = None   # latest payload not yet sent
    due: float = 0.0                # monotonic time to send `pending`
    sent: Optional[str] = None      # last payload HA accepted
    sent_at: float = 0.0
    playing: bool = False           # state of the last payload offered


class Notifier:
    def __init__(self, cfg: Config, covers: Optional[Covers] = None) -> None:
        self.cfg = cfg
        self.covers = covers or Covers("")
        self.slots: dict[str, Slot] = {}

    def wanted(self, blob: dict) -> bool:
        if self.cfg.sources is None:
            return True
        return (str(blob.get("source", "")).lower() in self.cfg.sources
                or str(blob.get("source_byte", "")).lower() in self.cfg.sources
                # "ml_listen" in sources: every source the bus listener reports
                or (blob.get("origin") == "ml_listen" and "ml_listen" in self.cfg.sources))

    def offer(self, raw: str, *, now: float, immediate: bool = False) -> None:
        """A new payload from redis. Schedules it for sending."""
        try:
            blob = json.loads(raw)
        except ValueError:
            log(f"ignoring non-JSON message: {raw[:80]!r}")
            return
        if not isinstance(blob, dict) or not self.wanted(blob):
            return
        # HA can't use a path on the Pi: swap art_url for a fetchable URL.
        blob["cover_url"] = self.covers.url_for(str(blob.pop("art_url", "") or ""))
        key = str(blob.get("source_byte") or blob.get("source"))
        slot = self.slots.setdefault(key, Slot())
        playing = blob.get("state") == "playing"
        if immediate:
            delay = 0.0
        elif slot.playing and not playing:
            delay = max(self.cfg.debounce_ms, self.cfg.idle_debounce_ms) / 1000
        else:
            delay = self.cfg.debounce_ms / 1000
        slot.playing = playing
        # Canonical form so equal states compare equal.
        slot.pending = json.dumps(blob, ensure_ascii=False, sort_keys=True)
        slot.due = now + delay

    def tick(self, now: float) -> None:
        """Send whatever is due."""
        for key, slot in self.slots.items():
            if slot.pending is not None and now >= slot.due:
                payload, slot.pending = slot.pending, None
                if payload == slot.sent:
                    continue                    # flapped back; HA has it
                self._send(slot, payload, now)
            elif (slot.pending is None and slot.sent is not None
                  and self.cfg.keepalive_s > 0
                  and now - slot.sent_at >= self.cfg.keepalive_s):
                self._send(slot, slot.sent, now)

    def _send(self, slot: Slot, payload: str, now: float) -> None:
        req = urllib.request.Request(
            self.cfg.url, data=payload.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as r:
                r.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log(f"POST failed ({e}); retrying in {self.cfg.retry_s:.0f} s")
            slot.pending, slot.due = payload, now + self.cfg.retry_s
            # Unknown what HA holds now -- make sure the retry isn't
            # swallowed as a duplicate.
            slot.sent = None
            return
        if payload != slot.sent:            # keepalives stay quiet
            blob = json.loads(payload)
            log(f"sent {blob.get('source')}: {blob.get('state')} "
                f"{blob.get('provider') or '-'} {blob.get('title', '')!r}")
        slot.sent, slot.sent_at = payload, now


# ---- main -------------------------------------------------------------------

def restart_listener(host: str, port: int, stop: list, restart: list) -> None:
    """Thread: end the process when mdt-web asks for a restart."""
    while not stop[0]:
        try:
            ps = redis.StrictRedis(host=host, port=port, decode_responses=True
                                   ).pubsub(ignore_subscribe_messages=True)
            ps.subscribe(CTL_RESTART)
            while not stop[0]:
                m = ps.get_message(timeout=1.0)
                if m and m.get("data") in ("ha-notifier", "all"):
                    log("restart requested (config changed)")
                    restart[0] = stop[0] = True
                    return
        except redis.exceptions.RedisError:
            time.sleep(2.0)


def run(cfg: Config, stop: list[bool],
        covers: Optional[Covers] = None) -> None:
    n = Notifier(cfg, covers)
    while not stop[0]:
        r = redis.StrictRedis(host=cfg.redis_host, port=cfg.redis_port,
                              db=0, decode_responses=True)
        ps = r.pubsub(ignore_subscribe_messages=True)
        try:
            # Subscribe first, then read the hash: a change landing in
            # between shows up in both, which the dedup absorbs.
            ps.subscribe(NOWPLAYING_CHAN)
            now = time.monotonic()
            for raw in (r.hgetall(NOWPLAYING_KEY) or {}).values():
                n.offer(raw, now=now, immediate=True)
            log(f"listening on {NOWPLAYING_CHAN} -> {cfg.url.split('/api/')[0]}")
            while not stop[0]:
                msg = ps.get_message(timeout=0.2)
                now = time.monotonic()
                if msg and msg.get("type") == "message":
                    n.offer(msg["data"], now=now)
                n.tick(now)
        except redis.exceptions.RedisError as e:
            log(f"redis error: {e}; reconnecting in 2 s")
            time.sleep(2.0)
        finally:
            try:
                ps.close()
                r.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"TOML config with an [ha_notifier] table "
                         f"(default {DEFAULT_CONFIG})")
    args = ap.parse_args()

    cfg = load_config(args.config)

    stop, restart = [False], [False]
    def _stop(*_):
        stop[0] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if cfg is None:
        log(f"[ha_notifier] not enabled in {args.config} -- idle until the "
            f"config changes")
        with open(args.config, "rb") as f:
            raw = tomllib.load(f)
        restart_listener(str(raw.get("redis_host", "localhost")),
                         int(raw.get("redis_port", 6379)), stop, restart)
        return RESTART_EXIT if restart[0] else 0

    threading.Thread(target=restart_listener,
                     args=(cfg.redis_host, cfg.redis_port, stop, restart),
                     name="ctl-restart", daemon=True).start()

    covers = None
    if cfg.cover_port:
        covers = Covers(cfg.cover_base_url)
        try:
            serve_covers(covers, cfg.cover_port)
        except OSError as e:
            log(f"can't serve cover art on port {cfg.cover_port}: {e} "
                f"-- continuing without local covers")
            covers = None

    run(cfg, stop, covers)
    return RESTART_EXIT if restart[0] else 0


if __name__ == "__main__":
    sys.exit(main())
