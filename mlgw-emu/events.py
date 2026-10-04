"""MLGW-protocol events derived from ML bus traffic.

A real MLGW tells Home Assistant about things on the bus with its own
messages. The emulation derives them from the same telegrams, following
what was observed on a real system (BeoVision 10 as video master,
BeoSound 3200 as audio master, link rooms):

  0x04 Light/Control  every VIRTUAL_BEO4 key a master sends to the MLGW
                      address 0xF0 -- including the LIGHT key itself and
                      Key Release -- one event each. Room = the room of the
                      sending device (a link-room product only forwards if
                      it supports LIGHT; a BeoVision 6 and BeoLab 2000 don't).
  0x05 All standby    RELEASE (or STANDBY) from a master to ALL (0x80).
  0x02 Source status  SOURCE STATUS (0x87) a master sends to 0xF0 -> status
                      of that master's MLN. That's all a real MLGW reports:
                      link rooms don't address the MLGW, and HA's integration
                      tracks them from the ML log itself.
                      extended=True adds link rooms: GOTO_SOURCE from a device
                      -> on that source, any SOURCE STATUS for a source ->
                      every device on it, RELEASE from it -> standby.
  0x03 Pict&sound     PICT/SOUND STATUS (0x98), which a master sends to the
                      MLGW address: volume (payload byte 3 -- seen counting
                      20..28 while the volume was raised) and speaker mode
                      (byte 1). The other bytes aren't confirmed yet and go
                      out as 0 (not muted, screens inactive) -- the
                      integration turns a player on when a screen is
                      reported active, so a wrong guess would be worse.

Telegrams arrive without checksum / end marker: to, from, 0x01, type,
src_dest, orig_src, 0x00, payload type, payload length, payload...
"""
from __future__ import annotations

from typing import Callable, Optional

ADDR_ALL = 0x80
ADDR_MLGW = 0xF0
MASTERS = (0xC0, 0xC1)

PT_BEO4_KEY = 0x0D
PT_STANDBY = 0x10
PT_RELEASE = 0x11
PT_VIRTUAL_BEO4 = 0x20
PT_GOTO_SOURCE = 0x45
PT_SOURCE_STATUS = 0x87
PT_PICT_SOUND = 0x98

LC_LIGHT = 0x01                                       # Light/Control type

ACT_UNKNOWN = 0x00
ACT_PLAYING = 0x02
ACT_STANDBY = 0x06


def mlgw_frame(msg_type: int, payload: bytes) -> bytes:
    return bytes([0x01, msg_type, len(payload), 0x00]) + payload


class EventSynth:
    """Feed every bus telegram; emits MLGW frames via `emit(frame)`.
    `lookup(addr)` -> (MLN, room number, room name) or None."""

    def __init__(self, lookup: Callable[[int], Optional[tuple]],
                 emit: Callable[[bytes], None],
                 log: Callable[[str], None] = print,
                 extended: bool = False) -> None:
        self.lookup = lookup
        self.extended = extended
        self.emit = emit
        self.log = log
        self.on_source: dict[int, int] = {}          # MLN -> source byte
        self.status: dict[int, tuple] = {}           # source -> (medium, track, activity, picture)
        self.last_key: Optional[dict] = None         # latest Beo4 key seen (UI "identify")
        self._sent: dict[tuple, bytes] = {}          # (type, MLN) -> last status frame

    # ---- event builders ----------------------------------------------------

    def _emit_status(self, key: tuple, frame: bytes) -> None:
        """Status frames only on change: the bus often repeats a status
        telegram (to several addresses), a real MLGW reports changes."""
        if self._sent.get(key) != frame:
            self._sent[key] = frame
            self.emit(frame)

    def _source_status(self, mln: int, source: int) -> None:
        medium, track, activity, picture = self.status.get(
            source, (0, 0, ACT_UNKNOWN, 0))
        if source == 0:
            activity = ACT_STANDBY
        self._emit_status((0x02, mln), mlgw_frame(0x02, bytes([
            mln, source, (medium >> 8) & 0xFF, medium & 0xFF,
            (track >> 8) & 0xFF, track & 0xFF, activity, picture])))

    # ---- feed --------------------------------------------------------------

    def feed(self, t: bytes, now: float = 0.0) -> None:
        if len(t) < 9:
            return
        to, frm, pt = t[0], t[1], t[7]
        try:
            if pt == PT_VIRTUAL_BEO4 and to == ADDR_MLGW and len(t) >= 15:
                self._light(frm, t[14])
            elif pt in (PT_RELEASE, PT_STANDBY) and to == ADDR_ALL and frm in MASTERS:
                self.log("all standby")
                self.on_source.clear()
                self._sent.clear()
                self.emit(mlgw_frame(0x05, b""))
            elif pt == PT_RELEASE and self.extended:
                dev = self.lookup(frm)
                if dev and self.on_source.pop(dev[0], None) is not None:
                    self._source_status(dev[0], 0)
            elif pt == PT_GOTO_SOURCE and len(t) >= 13 and self.extended:
                dev = self.lookup(frm)
                if dev:
                    self.on_source[dev[0]] = t[11]
                    self._source_status(dev[0], t[11])
            elif pt == PT_SOURCE_STATUS and len(t) >= 23 and (
                    to == ADDR_MLGW or self.extended):
                self._status(frm, to, t)
            elif pt == PT_PICT_SOUND and len(t) >= 14 and (
                    to == ADDR_MLGW or self.extended):
                dev = self.lookup(frm)
                if dev:
                    # mln, sound, speaker mode, volume, scr1 mute/active,
                    # scr2 mute/active, cinema, stereo
                    self._emit_status((0x03, dev[0]), mlgw_frame(0x03, bytes([
                        dev[0], 0x00, t[10], t[12], 0, 0, 0, 0, 0, 0])))
            elif pt == PT_BEO4_KEY and len(t) >= 12:
                self.last_key = {"address": frm, "source": t[10],
                                 "key": t[11], "time": now}
        except Exception as e:                       # never break the feed
            self.log(f"event synth: {e} on {t.hex()}")

    def _light(self, frm: int, key: int) -> None:
        dev = self.lookup(frm)
        room = dev[1] if dev else 0
        self.emit(mlgw_frame(0x04, bytes([room, LC_LIGHT, key])))

    def _status(self, frm: int, to: int, t: bytes) -> None:
        source = t[10]
        medium = t[18] * 256 + t[17]
        track = t[19] if t[8] < 27 or len(t) < 38 else t[36] * 256 + t[37]
        self.status[source] = (medium, track, t[21], t[22])
        dev = self.lookup(frm)
        if to == ADDR_MLGW:
            # A master telling the MLGW about its own source.
            if dev:
                self.on_source[dev[0]] = source
                self._source_status(dev[0], source)
            if not self.extended:
                return
        elif dev and frm in MASTERS:
            self.on_source[dev[0]] = source
        for mln, src in list(self.on_source.items()):
            if src == source:
                self._source_status(mln, source)
