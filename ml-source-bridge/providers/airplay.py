"""AirPlay provider via shairport-sync.

Talks to shairport-sync over D-Bus by shelling out to `dbus-send`. Uses
the legacy `org.gnome.ShairportSync.RemoteControl` interface for transport
control (Play/Pause/Next/Previous) and `Properties.Get` for metadata.

Setup gotcha: shairport-sync 4.3.x tries to claim its bus name with a
`.i<pid>` suffix that the packaged D-Bus policy doesn't allow. Install
the override at `scripts/shairport-sync-instance-policy.conf` to fix it,
otherwise dbus-send calls fail with `ServiceUnknown`.

We keep the dbus-send shellouts (no extra Python dep). The whole thing is
quarantined in this one file, so a future swap to dbus-next is contained.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Optional

from core.bus import log
from providers.base import Metadata, SourceProvider


# Stable D-Bus addresses for shairport-sync.
# Two interfaces are exposed by shairport-sync 4.x:
#   - legacy `org.gnome.ShairportSync` -- transport control (Play/Pause/Next/Previous)
#   - standard MPRIS `org.mpris.MediaPlayer2.ShairportSync` -- properties incl.
#     PlaybackStatus, Metadata
# We use legacy for transport (next/prev/etc. -- well-tested path) and MPRIS
# for state queries (PlaybackStatus is the canonical "is the session active"
# signal -- ALSA RUNNING lags behind it because audio buffers before
# flowing to the DAC).
_DEST   = "org.gnome.ShairportSync"
_PATH   = "/org/gnome/ShairportSync"
_IFACE  = "org.gnome.ShairportSync.RemoteControl"

_MPRIS_DEST  = "org.mpris.MediaPlayer2.ShairportSync"
_MPRIS_PATH  = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

_PLAYBACK_STATUS_RE = re.compile(r'variant\s+string\s+"(.*?)"')

# How long to keep ALSA muted after a skip (covers the AirPlay buffer drain).
_SKIP_MUTE_SECONDS = 2.1
_ALSA_MIXER_NAME   = "Digital"

# Don't fire the same transport command more often than this. The AM/VM
# emits multiple RELEASE telegrams in quick succession during teardown;
# without debouncing we'd fire dbus-send four times in 30 ms.
_TRANSPORT_DEBOUNCE_S = 0.5

# Regexes for `dbus-send Properties.Get Metadata` text output. The MPRIS
# Metadata dict contains:
#   xesam:title  -- string
#   xesam:album  -- string
#   xesam:artist -- array of string  (we take the first)
#   xesam:genre  -- array of string  (we take the first)
_TITLE_RE  = re.compile(r'string "xesam:title"\s+variant\s+string "(.*?)"')
_ALBUM_RE  = re.compile(r'string "xesam:album"\s+variant\s+string "(.*?)"')
_ARTIST_RE = re.compile(
    r'string "xesam:artist"\s+variant\s+array\s*\[\s*string "(.*?)"')
_GENRE_RE  = re.compile(
    r'string "xesam:genre"\s+variant\s+array\s*\[\s*string "(.*?)"')


def _dbus_send(method: str, *, timeout: float = 2.0) -> Optional[str]:
    """Call a D-Bus method on shairport-sync and return stdout (or None on
    failure). `method` is the trailing method name, e.g. 'Next'."""
    cmd = [
        "dbus-send", "--system", "--print-reply", "--type=method_call",
        f"--dest={_DEST}", _PATH, f"{_IFACE}.{method}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"[airplay] dbus-send {method} failed: {e}", err=True)
        return None
    if r.returncode != 0:
        # First line of stderr usually has the most useful info.
        err_msg = r.stderr.strip().split("\n", 1)[0]
        if "ServiceUnknown" in err_msg:
            err_msg += (" -- install scripts/"
                        "shairport-sync-instance-policy.conf and reload dbus")
        log(f"[airplay] dbus-send {method}: rc={r.returncode} {err_msg}",
            err=True)
        return None
    return r.stdout


def _dbus_get_metadata(timeout: float = 2.0) -> Optional[str]:
    cmd = [
        "dbus-send", "--system", "--print-reply",
        f"--dest={_DEST}", _PATH,
        "org.freedesktop.DBus.Properties.Get",
        f"string:{_IFACE}", "string:Metadata",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return r.stdout if r.returncode == 0 else None


def _dbus_get_mpris_playback_status(timeout: float = 2.0) -> Optional[str]:
    """Returns shairport-sync's MPRIS PlaybackStatus property -- one of
    'Playing', 'Paused', 'Stopped' -- or None on failure (shairport
    down, dbus error, etc.)."""
    cmd = [
        "dbus-send", "--system", "--print-reply",
        f"--dest={_MPRIS_DEST}", _MPRIS_PATH,
        "org.freedesktop.DBus.Properties.Get",
        f"string:{_MPRIS_PLAYER_IFACE}", "string:PlaybackStatus",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    m = _PLAYBACK_STATUS_RE.search(r.stdout)
    return m.group(1) if m else None


def _check_dbus_reachable() -> bool:
    """Return True if shairport-sync's D-Bus name is currently registered.
    Used at provider startup for an early, friendly error rather than
    letting every transport call fail with ServiceUnknown."""
    try:
        r = subprocess.run(
            ["dbus-send", "--system", "--print-reply",
             "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus",
             "org.freedesktop.DBus.NameHasOwner", f"string:{_DEST}"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "boolean true" in r.stdout


def _alsa_set(mute: bool) -> None:
    """`amixer set Digital mute|unmute`. Best-effort; failures logged."""
    state = "mute" if mute else "unmute"
    try:
        subprocess.run(["amixer", "set", _ALSA_MIXER_NAME, state],
                       capture_output=True, check=False, timeout=2.0)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"[airplay] amixer {state}: {e}", err=True)


# ----------------------------------------------------------------------------

class AirPlayProvider(SourceProvider):
    """shairport-sync AirPlay receiver, exposed as one ML source.

    The source byte and display name are caller-supplied so this provider
    can stand in for N.MUSIC (0x7A), CD (0x8D), N.RADIO (0xA1), etc.
    """

    def __init__(self, source_byte: int, display_name: str) -> None:
        self.source_byte = source_byte
        self.display_name = display_name
        self._mute_event = threading.Event()
        self._mute_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_call: dict[str, float] = {}    # method name -> monotonic
        self._call_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not _check_dbus_reachable():
            log(f"[airplay] WARNING: D-Bus name {_DEST!r} is not registered. "
                f"Install scripts/shairport-sync-instance-policy.conf and "
                f"`systemctl reload dbus && systemctl restart shairport-sync` "
                f"-- transport control will not work until then.", err=True)
        if self._mute_thread is None or not self._mute_thread.is_alive():
            self._stop.clear()
            self._mute_thread = threading.Thread(
                target=self._mute_loop, name="airplay-mute", daemon=True)
            self._mute_thread.start()

    def stop(self) -> None:
        self.pause()
        self._stop.set()
        self._mute_event.set()   # wake the mute thread so it can exit

    # ---- transport ---------------------------------------------------------

    def play(self) -> None:
        if self._debounce("Play"):
            log("[airplay] PLAY")
            _dbus_send("Play")

    def pause(self) -> None:
        if self._debounce("Pause"):
            log("[airplay] PAUSE")
            _dbus_send("Pause")

    def next(self) -> None:
        if self._debounce("Next"):
            log("[airplay] NEXT")
            self._mute_event.set()      # cover the buffer-drain gap
            _dbus_send("Next")

    def prev(self) -> None:
        if self._debounce("Previous"):
            log("[airplay] PREV")
            self._mute_event.set()
            _dbus_send("Previous")

    # ---- private ------------------------------------------------------------

    def _debounce(self, method: str) -> bool:
        """Return True if `method` should fire now, False if it was called
        within _TRANSPORT_DEBOUNCE_S. Used to absorb the AM/VM's repeated
        RELEASE telegrams during source teardown."""
        now = time.monotonic()
        with self._call_lock:
            last = self._last_call.get(method, 0.0)
            if (now - last) < _TRANSPORT_DEBOUNCE_S:
                return False
            self._last_call[method] = now
        return True

    # ---- introspection ------------------------------------------------------

    def is_playing(self) -> bool:
        """True when shairport-sync's MPRIS PlaybackStatus is 'Playing'.

        Why MPRIS, not ALSA RUNNING:
        ALSA pcm0p only goes RUNNING once audio is actually flowing to
        the DAC, which on a B&O bus means AFTER the bus has been woken
        and source-claim handshake is done. But our wake needs to fire
        BEFORE that to bring the bus up. Polling ALSA gave us a Catch-22
        (ALSA RUNNING -> wake; wake -> system on; system on -> audio
        flows -> ALSA RUNNING).

        MPRIS PlaybackStatus flips to 'Playing' the moment shairport-sync
        accepts an inbound AirPlay session, which is exactly the right
        edge to trigger our wake on.

        Falls back to ALSA RUNNING if the MPRIS query fails (shairport
        not running, dbus issue, etc.) so we degrade gracefully.
        """
        status = _dbus_get_mpris_playback_status()
        if status is not None:
            return status == "Playing"
        # Fallback: ALSA RUNNING (less accurate but works if MPRIS down)
        try:
            with open("/proc/asound/card0/pcm0p/sub0/status", "r") as f:
                return "RUNNING" in f.read()
        except FileNotFoundError:
            return False

    def metadata(self) -> Optional[Metadata]:
        out = _dbus_get_metadata()
        if not out:
            return None
        title  = _TITLE_RE.search(out)
        album  = _ALBUM_RE.search(out)
        artist = _ARTIST_RE.search(out)
        genre  = _GENRE_RE.search(out)
        if not (title or album or artist or genre):
            return None
        return Metadata(
            title  = title.group(1)  if title  else None,
            album  = album.group(1)  if album  else None,
            artist = artist.group(1) if artist else None,
            genre  = genre.group(1)  if genre  else None,
        )

    def _mute_loop(self) -> None:
        """Event-driven mute: wait for an event, mute ALSA, hold for the
        buffer-drain interval, unmute. No tight polling."""
        while not self._stop.is_set():
            if not self._mute_event.wait(timeout=None):
                continue
            self._mute_event.clear()
            if self._stop.is_set():
                return
            _alsa_set(mute=True)
            self._stop.wait(_SKIP_MUTE_SECONDS)
            _alsa_set(mute=False)
