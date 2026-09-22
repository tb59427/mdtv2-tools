"""MPD (Music Player Daemon) provider.

MPD is a long-running audio backend that renders a playlist to an ALSA
output. Unlike shairport-sync and sendspin -- both of which are
receivers driven by an external sender -- MPD is *itself* the source
of the audio; it owns the queue, decides what plays next, and streams
frames to the sound card on its own schedule. All we do here is drive
the transport (Play/Pause/Next/Previous) and read the current-song
metadata + playback state.

Wire protocol: plain TCP on port 6600, line-delimited text. We speak
it directly with the stdlib `socket` module -- no python-mpd2 dep, no
subprocess, no D-Bus. A tiny per-call helper opens a fresh connection,
sends one command, reads until `OK\\n` (or `ACK …\\n`), and returns
the raw response. MPD's per-command latency at localhost is well under
a millisecond, so the per-call overhead is negligible even at the
1 Hz poll rate of the Multi-Provider's own watcher.

Config lives in a `[mpd]` table alongside `[turntable]`:

    [mpd]
    host     = "localhost"     # default
    port     = 6600            # default
    password = ""              # optional; MPD's `password` command
"""
from __future__ import annotations

import re
import socket
from typing import Optional

from core.bus import log
from providers.base import Metadata, SourceProvider


# TCP timeout on each MPD command. Localhost is instant; anything
# slower than this and something is wrong.
_MPD_TIMEOUT_S = 2.0

# Debounce for transport commands (mirrors airplay/sendspin so the AM/VM
# repeat-teardown doesn't fire the same command four times in 30 ms).
_TRANSPORT_DEBOUNCE_S = 0.5

# MPD status/currentsong reply parsers.
_STATE_RE  = re.compile(r"^state:\s*(\S+)", re.MULTILINE)
_TITLE_RE  = re.compile(r"^Title:\s*(.+)$",  re.MULTILINE)
_ARTIST_RE = re.compile(r"^Artist:\s*(.+)$", re.MULTILINE)
_ALBUM_RE  = re.compile(r"^Album:\s*(.+)$",  re.MULTILINE)
_GENRE_RE  = re.compile(r"^Genre:\s*(.+)$",  re.MULTILINE)


class MpdProvider(SourceProvider):
    """MPD (Music Player Daemon) as one ML source.

    Caller supplies source_byte + display_name (typical: 0x7A N.MUSIC
    or 0xA1 N.RADIO with display "MPD" / "Netradio" / whatever fits).
    """

    def __init__(
        self,
        source_byte: int,
        display_name: str,
        *,
        host: str = "localhost",
        port: int = 6600,
        password: str = "",
    ) -> None:
        self.source_byte = source_byte
        self.display_name = display_name
        self._host = host
        self._port = port
        self._password = password
        self._last_call: dict[str, float] = {}
        import threading
        self._call_lock = threading.Lock()

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # No persistent connection to hold open -- each command opens
        # its own. Just poke the daemon once to give a friendly
        # startup log if it isn't reachable.
        r = self._cmd("ping")
        if r is None:
            log(f"[mpd] WARNING: {self._host}:{self._port} not reachable at "
                f"startup. Transport commands will fail silently until MPD "
                f"comes up.", err=True)
        else:
            log(f"[mpd] connected to {self._host}:{self._port}")

    # stop(): base class defaults to self.pause() which is fine here.

    # ---- transport --------------------------------------------------------

    def play(self) -> None:
        if self._debounce("Play"):
            log("[mpd] PLAY")
            self._cmd("play")

    def pause(self) -> None:
        if self._debounce("Pause"):
            log("[mpd] PAUSE")
            self._cmd("pause 1")

    def next(self) -> None:
        if self._debounce("Next"):
            log("[mpd] NEXT")
            self._cmd("next")

    def prev(self) -> None:
        if self._debounce("Previous"):
            log("[mpd] PREV")
            self._cmd("previous")

    # ---- introspection ----------------------------------------------------

    def is_playing(self) -> bool:
        """True iff MPD reports `state: play` in its status response.
        `pause` and `stop` both count as not playing (Multi's last-
        writer-wins logic then correctly hands the source over when
        another sub takes over)."""
        out = self._cmd("status")
        if not out:
            return False
        m = _STATE_RE.search(out)
        return m is not None and m.group(1) == "play"

    def metadata(self) -> Optional[Metadata]:
        out = self._cmd("currentsong")
        if not out:
            return None
        title  = _TITLE_RE.search(out)
        artist = _ARTIST_RE.search(out)
        album  = _ALBUM_RE.search(out)
        genre  = _GENRE_RE.search(out)
        if not (title or artist or album or genre):
            return None
        return Metadata(
            title  = title.group(1).strip()  if title  else None,
            artist = artist.group(1).strip() if artist else None,
            album  = album.group(1).strip()  if album  else None,
            genre  = genre.group(1).strip()  if genre  else None,
        )

    # ---- private ----------------------------------------------------------

    def _debounce(self, method: str) -> bool:
        import time
        now = time.monotonic()
        with self._call_lock:
            last = self._last_call.get(method, 0.0)
            if (now - last) < _TRANSPORT_DEBOUNCE_S:
                return False
            self._last_call[method] = now
        return True

    def _cmd(self, cmd: str) -> Optional[str]:
        """Open a fresh TCP connection, send one command, read the
        reply, close. Returns response body (without OK/ACK trailer)
        or None on any error. Response ends with `OK\\n` (success) or
        starts with `ACK ` (error)."""
        try:
            with socket.create_connection(
                (self._host, self._port), timeout=_MPD_TIMEOUT_S
            ) as s:
                # MPD sends `OK MPD <version>\n` on connect; drain it.
                banner = self._recv_line(s)
                if not banner or not banner.startswith(b"OK"):
                    log(f"[mpd] unexpected banner from {self._host}:"
                        f"{self._port}: {banner!r}", err=True)
                    return None
                if self._password:
                    s.sendall(f"password {self._password}\n".encode())
                    ack = self._read_response(s)
                    if ack is None:
                        return None
                s.sendall((cmd + "\n").encode())
                return self._read_response(s)
        except (OSError, socket.timeout) as e:
            # Rate-limited log: a down MPD would spam this every poll.
            # Debounce via last_call by tagging the failed command name.
            if self._debounce(f"_err:{cmd.split()[0]}"):
                log(f"[mpd] {cmd!r} failed: {e}", err=True)
            return None

    @staticmethod
    def _recv_line(s: socket.socket) -> bytes:
        """Read until newline. MPD lines are short; no huge-buffer risk."""
        chunks = []
        while True:
            b = s.recv(1)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
            if b == b"\n":
                return b"".join(chunks)

    @classmethod
    def _read_response(cls, s: socket.socket) -> Optional[str]:
        """Read MPD response until a line starting with 'OK' (success)
        or 'ACK ' (error). Returns the body text (excluding the OK/ACK
        line) on success, None on ACK."""
        body_lines: list[str] = []
        while True:
            line = cls._recv_line(s)
            if not line:
                return None  # connection closed mid-response
            decoded = line.decode(errors="replace").rstrip("\n")
            if decoded == "OK":
                return "\n".join(body_lines)
            if decoded.startswith("ACK "):
                # Log the error once; caller gets None so it can no-op.
                log(f"[mpd] {decoded}", err=True)
                return None
            body_lines.append(decoded)
