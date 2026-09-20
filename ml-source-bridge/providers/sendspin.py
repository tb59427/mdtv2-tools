"""Sendspin provider via the sendspin daemon's MPRIS interface.

Sendspin is the Open Home Foundation's synchronized multi-room audio
protocol. On the Pi it runs as `sendspin daemon` (systemd) and pipes
audio to the local default ALSA device, which -- in our case -- feeds
the DAC that sits behind the Masterlink bus.

D-Bus wiring, the fiddly bits:

  * sendspin runs as a system-systemd unit but with `User=sendspin`,
    which means it registers MPRIS on the *user session bus* of that
    UID (typically /run/user/<uid>/bus), not on the system bus like
    shairport-sync. So every dbus-send call has to be routed through
    that user's session bus.

  * The session-bus socket is 0700 to the owner (`sendspin`); the
    ml-source-bridge process (typically root or pi) cannot open it
    directly. We wrap each dbus-send in `sudo -u <user> sh -c '…'`
    with the right env vars. Cheap; no extra Python deps.

  * Sendspin's MPRIS name carries a per-PID suffix, e.g.
    `org.mpris.MediaPlayer2.Sendspin.instance59584`. This changes on
    every daemon restart, so we can't hard-code it -- we ask the bus
    for the current well-known name each time we discover, and cache
    it until a call fails. Same pattern shairport 4.3.x needs, only
    there we sidestep it via the legacy `org.gnome.ShairportSync`
    non-instance interface.

  * Also needs `loginctl enable-linger <user>` on the Pi so the user
    manager -- and therefore the session bus -- starts at boot and
    outlives any interactive login.
"""
from __future__ import annotations

import pwd
import re
import subprocess
import threading
import time
from typing import Optional

from core.bus import log
from providers.base import Metadata, SourceProvider


# ---------------------------------------------------------------------------
# D-Bus / user-session wiring.

# Unix user the sendspin daemon runs under. Everything else (uid, bus
# socket path, dbus-send `sudo -u`) is derived from this. Change here if
# your systemd unit uses a different user.
_SENDSPIN_USER = "sendspin"

# MPRIS well-known name prefix. The daemon appends `.instance<pid>` so
# we match on the prefix and let discovery latch onto the current name.
_DEST_PREFIX = "org.mpris.MediaPlayer2.Sendspin"

_MPRIS_PATH         = "/org/mpris/MediaPlayer2"
_MPRIS_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

_PLAYBACK_STATUS_RE = re.compile(r'variant\s+string\s+"(.*?)"')

# Note: no ALSA-mute-on-skip logic here (unlike the AirPlay provider).
# The sendspin daemon flushes its own buffer on track change cleanly, so
# the extra mute just risks leaving the mixer stuck muted if the unmute
# call ever fails. If a real audible tail from the previous track shows
# up on skip, revisit -- but put the unmute in a try/finally then.

# Debounce for transport commands. The AM/VM emits multiple RELEASE
# telegrams in quick succession during source teardown; without this
# we'd fire dbus-send four times in 30 ms.
_TRANSPORT_DEBOUNCE_S = 0.5

# MPRIS Metadata dict regexes (same shape as the airplay provider).
_TITLE_RE  = re.compile(r'string "xesam:title"\s+variant\s+string "(.*?)"')
_ALBUM_RE  = re.compile(r'string "xesam:album"\s+variant\s+string "(.*?)"')
_ARTIST_RE = re.compile(
    r'string "xesam:artist"\s+variant\s+array\s*\[\s*string "(.*?)"')
_GENRE_RE  = re.compile(
    r'string "xesam:genre"\s+variant\s+array\s*\[\s*string "(.*?)"')

# `dbus-send ListNames` returns each name inside `string "..."`.
_LIST_NAMES_RE = re.compile(r'string "(.*?)"')


def _resolve_uid(user: str) -> Optional[int]:
    """Resolve `user` to its UID, or None if the account isn't there.
    Called once at import so the module fails loud early if `sendspin`
    doesn't exist on this box (missing install)."""
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


_SENDSPIN_UID = _resolve_uid(_SENDSPIN_USER)
_XDG_RUNTIME  = f"/run/user/{_SENDSPIN_UID}"      if _SENDSPIN_UID else ""
_DBUS_ADDR    = f"unix:path=/run/user/{_SENDSPIN_UID}/bus" if _SENDSPIN_UID else ""


# ----------------------------------------------------------------------------

class SendspinProvider(SourceProvider):
    """sendspin daemon receiver, exposed as one ML source.

    Same shape as AirPlayProvider: caller supplies source byte + display
    name so this provider can stand in for N.MUSIC (0x7A), N.RADIO
    (0xA1), etc.
    """

    def __init__(self, source_byte: int, display_name: str) -> None:
        self.source_byte = source_byte
        self.display_name = display_name
        # Cached MPRIS well-known name including the .instance<pid>
        # suffix. Re-discovered on start() and whenever a call fails
        # (which typically means sendspin restarted with a new pid).
        self._dest: Optional[str] = None
        self._last_call: dict[str, float] = {}
        self._call_lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if _SENDSPIN_UID is None:
            log(f"[sendspin] WARNING: user {_SENDSPIN_USER!r} not found on "
                f"this system. Is `sendspin daemon` installed? Transport "
                f"control disabled until the account exists.", err=True)
        else:
            found = self._discover_dest()
            if found is None:
                log(f"[sendspin] WARNING: no MPRIS name matching "
                    f"{_DEST_PREFIX!r} on the {_SENDSPIN_USER!r} session "
                    f"bus. Sendspin registers MPRIS only when a stream is "
                    f"active -- transport control will latch on first "
                    f"successful call.", err=True)
            else:
                self._dest = found
                log(f"[sendspin] latched onto D-Bus name {found!r}")
    def stop(self) -> None:
        self.pause()

    # ---- transport ---------------------------------------------------------

    def play(self) -> None:
        if self._debounce("Play"):
            log("[sendspin] PLAY")
            self._dbus_call("Play")

    def pause(self) -> None:
        if self._debounce("Pause"):
            log("[sendspin] PAUSE")
            self._dbus_call("Pause")

    def next(self) -> None:
        if self._debounce("Next"):
            log("[sendspin] NEXT")
            self._dbus_call("Next")

    def prev(self) -> None:
        if self._debounce("Previous"):
            log("[sendspin] PREV")
            self._dbus_call("Previous")

    # ---- introspection ------------------------------------------------------

    def is_playing(self) -> bool:
        """True when sendspin's MPRIS PlaybackStatus is 'Playing'.
        Falls back to ALSA RUNNING when MPRIS can't be reached (daemon
        down between streams, dbus hiccup) so we degrade gracefully."""
        status = self._dbus_get_property("PlaybackStatus")
        if status is not None:
            return status == "Playing"
        try:
            with open("/proc/asound/card0/pcm0p/sub0/status", "r") as f:
                return "RUNNING" in f.read()
        except FileNotFoundError:
            return False

    def metadata(self) -> Optional[Metadata]:
        out = self._dbus_get_property_raw("Metadata")
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

    # ---- private: D-Bus plumbing on the user session bus -------------------

    def _sudo_env_prefix(self) -> list[str]:
        """`sudo -u sendspin sh -c '…'` wrapper. We use sh -c so the env
        vars are set in the same process as dbus-send (a plain
        `sudo -u user cmd` wouldn't propagate our env)."""
        return ["sudo", "-u", _SENDSPIN_USER, "sh", "-c"]

    def _run_dbus_send(self, dbus_args: list[str],
                       timeout: float = 2.0) -> Optional[subprocess.CompletedProcess]:
        """Build a dbus-send call, run it as the sendspin user with the
        session-bus env preset. Returns the CompletedProcess or None on
        subprocess exception."""
        if _SENDSPIN_UID is None:
            return None
        # sh-quoting is fine here: we control every arg, none contain
        # embedded single quotes.
        quoted = " ".join(f"'{a}'" for a in dbus_args)
        shell_cmd = (
            f"XDG_RUNTIME_DIR={_XDG_RUNTIME} "
            f"DBUS_SESSION_BUS_ADDRESS={_DBUS_ADDR} "
            f"dbus-send {quoted}"
        )
        try:
            return subprocess.run(
                self._sudo_env_prefix() + [shell_cmd],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            log(f"[sendspin] dbus-send exec failed: {e}", err=True)
            return None

    def _dbus_call(self, method: str, *, timeout: float = 2.0) -> Optional[str]:
        """Call Player.<method> on sendspin. Re-discovers the bus name
        on failure once, in case sendspin restarted with a new pid."""
        for attempt in range(2):
            dest = self._dest or self._discover_dest()
            if dest is None:
                log(f"[sendspin] {method}: no MPRIS name registered "
                    f"(sendspin idle or down)", err=True)
                return None
            self._dest = dest
            r = self._run_dbus_send([
                "--session", "--print-reply", "--type=method_call",
                f"--dest={dest}", _MPRIS_PATH,
                f"{_MPRIS_PLAYER_IFACE}.{method}",
            ], timeout=timeout)
            if r is None:
                return None
            if r.returncode == 0:
                return r.stdout
            # Failed -- most common cause is a stale .instance<pid>
            # name after sendspin restart. Invalidate cache, try once
            # more with fresh discovery.
            err_msg = r.stderr.strip().split("\n", 1)[0]
            if attempt == 0 and "ServiceUnknown" in err_msg:
                log(f"[sendspin] {method}: {dest!r} gone, re-discovering",
                    err=True)
                self._dest = None
                continue
            log(f"[sendspin] dbus-send {method}: rc={r.returncode} "
                f"{err_msg}", err=True)
            return None
        return None

    def _dbus_get_property_raw(self, name: str,
                               timeout: float = 2.0) -> Optional[str]:
        """Raw stdout of Properties.Get for `name` on Player iface."""
        for attempt in range(2):
            dest = self._dest or self._discover_dest()
            if dest is None:
                return None
            self._dest = dest
            r = self._run_dbus_send([
                "--session", "--print-reply",
                f"--dest={dest}", _MPRIS_PATH,
                "org.freedesktop.DBus.Properties.Get",
                f"string:{_MPRIS_PLAYER_IFACE}", f"string:{name}",
            ], timeout=timeout)
            if r is None:
                return None
            if r.returncode == 0:
                return r.stdout
            if attempt == 0 and "ServiceUnknown" in (r.stderr or ""):
                self._dest = None
                continue
            return None
        return None

    def _dbus_get_property(self, name: str) -> Optional[str]:
        out = self._dbus_get_property_raw(name)
        if not out:
            return None
        m = _PLAYBACK_STATUS_RE.search(out)
        return m.group(1) if m else None

    def _discover_dest(self, timeout: float = 2.0) -> Optional[str]:
        """Ask the sendspin user's session bus for its registered names
        and return the first one starting with `_DEST_PREFIX`. Returns
        None if nothing matches -- means sendspin isn't currently
        streaming (MPRIS only appears while a session is active)."""
        r = self._run_dbus_send([
            "--session", "--print-reply",
            "--dest=org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus.ListNames",
        ], timeout=timeout)
        if r is None or r.returncode != 0:
            return None
        names = _LIST_NAMES_RE.findall(r.stdout)
        for n in names:
            if n.startswith(_DEST_PREFIX):
                return n
        return None

    # ---- private: debounce --------------------------------------------------

    def _debounce(self, method: str) -> bool:
        now = time.monotonic()
        with self._call_lock:
            last = self._last_call.get(method, 0.0)
            if (now - last) < _TRANSPORT_DEBOUNCE_S:
                return False
            self._last_call[method] = now
        return True
