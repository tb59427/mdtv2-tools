#!/usr/bin/env python3
"""ml-debug -- decode MasterLink telegrams from the redis broker into
human-readable lines.

Subscribes to the broker's `link:ml:transmit` and `link:ml:receive` channels
and prints a decoded line per telegram. Pairs with `mdtv2-broker.py`.

Usage:
    ./ml_debug.py                      # both directions, color
    ./ml_debug.py --rx-only --no-color
    ./ml_debug.py --redis-host other.host

Dependencies: redis-py.
"""
from __future__ import annotations

import argparse
import datetime
import signal
import sys
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import redis

from const import (
    beo4_commanddict,
    ml_command_type_dict,
    ml_destselectordict,
    ml_pictureformatdict,
    ml_selectedsourcedict,
    ml_src_type_dict,
    ml_state_dict,
    ml_telegram_type_dict,
)


# ---------------- helpers ----------------

def lookup(d: Dict[int, str], key: int) -> str:
    return d.get(key, f"UNKNOWN(0x{key:02x})")


# ---------------- telegram model ----------------
#
# Wire layout (RX, from `link:ml:receive`):
#   [0]    TO
#   [1]    FROM
#   [2]    b2
#   [3]    type
#   [4]    src_dest
#   [5]    orig_src
#   [6]    b6
#   [7]    payload_type
#   [8]    N (payload length)
#   [9..]  N payload bytes
#   [N+9]  RESERVED
#   [N+10] checksum   (RX only -- TX excludes these last two)
#   [N+11] 0x00


@dataclass
class Telegram:
    direction: str                    # "rx" or "tx"
    timestamp: datetime.datetime
    raw: bytes

    @property
    def to_addr(self) -> int:        return self.raw[0]
    @property
    def from_addr(self) -> int:      return self.raw[1]
    @property
    def telegram_type(self) -> int:  return self.raw[3]
    @property
    def src_dest(self) -> int:       return self.raw[4]
    @property
    def orig_src(self) -> int:       return self.raw[5]
    @property
    def payload_type(self) -> int:   return self.raw[7]
    @property
    def payload_len(self) -> int:    return self.raw[8]

    @property
    def payload(self) -> bytes:
        n = self.payload_len
        return self.raw[9:9 + n]

    @classmethod
    def from_hex(cls, hex_str: str, direction: str) -> "Telegram":
        return cls(
            direction=direction,
            timestamp=datetime.datetime.now(),
            raw=bytes.fromhex(hex_str.strip()),
        )


# ---------------- payload decoders ----------------
#
# Each decoder receives the Telegram and returns a list of (label, value)
# tuples. They access raw bytes directly (the original code's indices are
# expressed against the full telegram, not just the payload slice -- we
# keep that convention for parity).

DecoderFn = Callable[[Telegram], List[Tuple[str, str]]]


def _need(t: Telegram, n_bytes: int) -> bool:
    return len(t.raw) >= n_bytes


def decode_source_status(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 23):
        return [("ERROR", f"telegram too short ({len(t.raw)} bytes)")]
    out = [
        ("SOURCE",        f"0x{t.raw[10]:02x} {lookup(ml_selectedsourcedict, t.raw[10])}"),
        ("LOCAL_SRC",     f"0x{t.raw[13]:02x}"),
        ("SRC_MEDIUM",    f"0x{t.raw[18]:02x}{t.raw[17]:02x}"),
    ]
    # CH/TRACK: layout differs based on payload length
    if t.payload_len < 27:
        ch_track = str(t.raw[19])
    elif _need(t, 38):
        ch_track = str(t.raw[36] * 256 + t.raw[37])
    else:
        ch_track = "?"
    out.append(("CH_TRACK", ch_track))
    if _need(t, 22):
        out.append(("ACTIVITY", f"0x{t.raw[21]:02x} {lookup(ml_state_dict, t.raw[21])}"))
    if _need(t, 23):
        out.append(("PICTURE_ID", f"0x{t.raw[22]:02x} {lookup(ml_pictureformatdict, t.raw[22])}"))
    return out


def decode_beo4_command(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 12):
        return [("ERROR", "telegram too short")]
    return [
        ("SOURCE", f"0x{t.raw[10]:02x} {lookup(ml_selectedsourcedict, t.raw[10])}"),
        ("BUTTON", f"0x{t.raw[11]:02x} {lookup(beo4_commanddict, t.raw[11])}"),
    ]


def decode_track_info_long(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 14):
        return [("ERROR", "telegram too short")]
    return [
        ("SOURCE",   f"0x{t.raw[11]:02x} {lookup(ml_selectedsourcedict, t.raw[11])}"),
        ("CH_TRACK", str(t.raw[12])),
        ("ACTIVITY", f"0x{t.raw[13]:02x} {lookup(ml_state_dict, t.raw[13])}"),
    ]


def decode_goto_source(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 13):
        return [("ERROR", "telegram too short")]
    return [
        ("SOURCE",   f"0x{t.raw[11]:02x} {lookup(ml_selectedsourcedict, t.raw[11])}"),
        ("CH_TRACK", str(t.raw[12])),
    ]


_TRACK_CHANGE_SUBTYPES = {
    0x05: "CURRENT_SOURCE",
    0x07: "CHANGE_SOURCE",
    0x09: "SOURCE_NOT_AVAILABLE",
}


def decode_track_change(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 12):
        return [("ERROR", "telegram too short")]
    sub = t.raw[9]
    src_byte = t.raw[11]
    kind = _TRACK_CHANGE_SUBTYPES.get(sub, "UNKNOWN")
    out = [("KIND", f"0x{sub:02x} {kind}")]
    if sub == 0x07 and _need(t, 23):
        out += [
            ("PREV_SOURCE", f"0x{src_byte:02x} {lookup(ml_selectedsourcedict, src_byte)}"),
            ("NEW_SOURCE",  f"0x{t.raw[22]:02x} {lookup(ml_selectedsourcedict, t.raw[22])}"),
        ]
    elif sub in (0x05, 0x09):
        label = "CURR_SOURCE" if sub == 0x05 else "REQ_SOURCE"
        out.append((label, f"0x{src_byte:02x} {lookup(ml_selectedsourcedict, src_byte)}"))
    return out


_LOCK_MANAGER_SUBTYPES = {
    0x01: ("REQUEST_KEY",        False),
    0x02: ("TRANSFER_KEY",       True),
    0x03: ("TRANSFER_IMPOSSIBLE", False),
    0x04: ("KEY_RECEIVED",       True),
    0x05: ("TIMEOUT",            False),
}


def decode_lockmanager(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 11):
        return [("ERROR", "telegram too short")]
    sub = t.raw[9]
    name, has_key = _LOCK_MANAGER_SUBTYPES.get(sub, ("UNDEFINED", False))
    out = [("KIND", f"0x{sub:02x} {name}")]
    if has_key:
        out.append(("KEY", f"0x{t.raw[10]:02x}"))
    return out


# ---- new decoders below this line (added by reverse-engineering catch-up) ----


def decode_master_present(t: Telegram) -> List[Tuple[str, str]]:
    """Periodic 'I'm here' beacon from a master device. Useful for discovering
    the bus topology -- watch the FROM addresses to enumerate devices."""
    return []


def _ascii_text(b: bytes) -> str:
    """Extract a printable ASCII run from a byte slice (drops control bytes
    and trailing 0x00 padding)."""
    return bytes(c for c in b if 0x20 <= c < 0x7F).decode("ascii", "replace").strip()


_DISPLAY_SOURCE_SUBTYPES = {
    0x01: "type1",
    0x02: "type2",
    0x03: "with_name",
}


def decode_display_source(t: Telegram) -> List[Tuple[str, str]]:
    """0x06 DISPLAY_SOURCE -- the master broadcasts what's currently shown on
    its display. Subtype 3 carries the printable source name as ASCII."""
    if not _need(t, 10):
        return [("ERROR", "telegram too short")]
    sub = t.raw[9]
    out = [("SUBTYPE", f"0x{sub:02x} {_DISPLAY_SOURCE_SUBTYPES.get(sub, 'unknown')}")]
    if sub == 0x03:
        end = min(9 + t.payload_len, len(t.raw))
        name = _ascii_text(t.raw[10:end])
        if name:
            out.append(("NAME", name))
    return out


_EXT_SOURCE_INFO_LABELS = {
    # Subtype meanings differ per source. We can't always know the source here,
    # so we use generic labels; the actual semantics are documented in the const
    # file (Radio: Genre/Country/RDS, A.MEM: Genre/Album/Artist/Track, ...).
    0x01: "FIELD1",
    0x02: "FIELD2",
    0x03: "FIELD3",
    0x04: "FIELD4",
    0x05: "BEO4_KEY",
    0x06: "FIELD6",
}


def decode_extended_source_info(t: Telegram) -> List[Tuple[str, str]]:
    """0x0B EXTENDED_SOURCE_INFORMATION -- subtypes 1..6 carry ASCII text
    starting at byte 14 (track / artist / album / RDS / etc., depending on
    subtype and source). Subtype 5 is a single Beo4-key code at byte 14."""
    if not _need(t, 14):
        return [("ERROR", "telegram too short")]
    sub = t.raw[9]
    label = _EXT_SOURCE_INFO_LABELS.get(sub, "TEXT")
    out = [("SUBTYPE", f"0x{sub:02x}")]
    end = min(9 + t.payload_len, len(t.raw))
    if sub == 0x05 and end > 14:
        btn = t.raw[14]
        out.append((label, f"0x{btn:02x} {lookup(beo4_commanddict, btn)}"))
    else:
        text = _ascii_text(t.raw[14:end])
        if text:
            out.append((label, text))
    return out


def decode_standby(t: Telegram) -> List[Tuple[str, str]]:
    """0x10 STANDBY -- request to enter standby. No interesting payload."""
    return []


def decode_release(t: Telegram) -> List[Tuple[str, str]]:
    """0x11 RELEASE -- device announces it's releasing the source / turning off."""
    return []


_REQUEST_LOCAL_SOURCE_SUBTYPES = {
    0x02: "QUERY",
    0x04: "NO_SOURCE",
    0x05: "SECONDARY_SOURCE",
    0x06: "PRIMARY_SOURCE",
}


def decode_request_local_source(t: Telegram) -> List[Tuple[str, str]]:
    """0x30 REQUEST_LOCAL_SOURCE -- query / response about the source playing
    locally on a device. Reply subtypes 5/6 carry a distribution bitmap (which
    output paths it goes to) and the source byte."""
    if not _need(t, 12):
        return [("ERROR", "telegram too short")]
    sub = t.raw[9]
    name = _REQUEST_LOCAL_SOURCE_SUBTYPES.get(sub, "UNKNOWN")
    out = [("KIND", f"0x{sub:02x} {name}")]
    if sub in (0x05, 0x06):
        bitmask = t.raw[10]
        bm_parts = []
        if bitmask & 0x01: bm_parts.append("coax")
        if bitmask & 0x02: bm_parts.append("ML_BUS")
        if bitmask & 0x08: bm_parts.append("screen")
        out.append(("DIST", "+".join(bm_parts) or "-"))
        out.append(("SOURCE", f"0x{t.raw[11]:02x} {lookup(ml_selectedsourcedict, t.raw[11])}"))
    return out


def decode_request_distributed_source(t: Telegram) -> List[Tuple[str, str]]:
    """0x08 REQUEST_DISTRIBUTED_SOURCE -- "what source is being distributed
    on the link?" Sent by a link-room device (and by us, the state-tracker
    startup query) as a REQUEST; the Audio Master answers a link device
    with a RESPONSE (pl_len>=5) carrying the distributed source byte at
    raw[13]. The Video Master answers pl_len=0 (a bare ack, no source).
    The CONFIG (0x5e) broadcast form is a device's periodic presence ping.
    """
    if t.payload_len >= 5 and _need(t, 14):
        return [("SOURCE",
                 f"0x{t.raw[13]:02x} {lookup(ml_selectedsourcedict, t.raw[13])}")]
    if t.telegram_type == 0x14:
        return [("INFO", "ack (no source reported)")]
    return [("INFO", "query / presence ping")]


def decode_distribution_request(t: Telegram) -> List[Tuple[str, str]]:
    """0x6C DISTRIBUTION_REQUEST -- bus arbitration / source distribution
    handshake. Just dump payload for now; subtype semantics aren't fully
    reverse-engineered."""
    if not _need(t, 10):
        return [("ERROR", "telegram too short")]
    return [
        ("SUBTYPE", f"0x{t.raw[9]:02x}"),
        ("DATA",    t.payload[1:].hex() or "-"),
    ]


def decode_clock(t: Telegram) -> List[Tuple[str, str]]:
    """0x40 CLOCK -- time/date sync. Format is not fully documented; dump raw."""
    if t.payload_len < 1:
        return []
    return [("DATA", t.payload.hex())]


def decode_pc_present(t: Telegram) -> List[Tuple[str, str]]:
    """0x96 PC_PRESENT -- a PC-link source announces presence."""
    return []


def decode_pict_sound_status(t: Telegram) -> List[Tuple[str, str]]:
    """0x98 PICT_SOUND_STATUS -- the master broadcasts current audio + picture
    state. Per const.py, payload bytes carry:
        [0] sound bits 0-1 (0=ok,1=muted), stereo mode bits 2-3
        [1] speaker mode
        [2] audio volume
        [3] picture format identifier
        [4] screen bitmap (mute/active per screen, cinema mode)
    The legacy decoder only read 2 of these; this version exposes all five."""
    if not _need(t, 14):
        # Fall back to the legacy short read if the full layout isn't present.
        if not _need(t, 13):
            return [("ERROR", "telegram too short")]
        return [
            ("MUTE",   f"0x{t.raw[10]:02x}"),
            ("VOLUME", f"0x{t.raw[12]:02x} ({t.raw[12]} dec)"),
        ]
    snd_byte = t.raw[9]
    sound_state = "muted" if (snd_byte & 0x03) else "ok"
    stereo      = (snd_byte >> 2) & 0x03
    return [
        ("SOUND",    sound_state),
        ("STEREO",   f"0x{stereo:02x}"),
        ("SPK_MODE", f"0x{t.raw[10]:02x}"),
        ("VOLUME",   f"{t.raw[11]} (0x{t.raw[11]:02x})"),
        ("PICT_FMT", f"0x{t.raw[12]:02x} {lookup(ml_pictureformatdict, t.raw[12])}"),
        ("SCREENS",  f"0x{t.raw[13]:02x}"),
    ]


def decode_mlgw_status(t: Telegram) -> List[Tuple[str, str]]:  # back-compat wrapper
    return decode_pict_sound_status(t)


def decode_virtual_beo4(t: Telegram) -> List[Tuple[str, str]]:
    if not _need(t, 15):
        return [("ERROR", "telegram too short")]
    return [
        ("COMMAND",     f"0x{t.raw[14]:02x} {lookup(beo4_commanddict, t.raw[14])}"),
        ("DEST_SELECT", f"0x{t.raw[11]:02x} {lookup(ml_destselectordict, t.raw[11])}"),
    ]


PAYLOAD_DECODERS: Dict[int, Tuple[str, DecoderFn]] = {
    0x04: ("master present",           decode_master_present),
    0x08: ("request distributed src",  decode_request_distributed_source),
    0x06: ("display source",           decode_display_source),
    0x0B: ("extended source info",     decode_extended_source_info),
    0x0D: ("beo4 command",             decode_beo4_command),
    0x10: ("standby",                  decode_standby),
    0x11: ("release",                  decode_release),
    0x20: ("virtual beo4 key",         decode_virtual_beo4),
    0x30: ("request local source",     decode_request_local_source),
    0x40: ("clock",                    decode_clock),
    0x44: ("track change info",        decode_track_change),
    0x45: ("go to source",             decode_goto_source),
    0x5C: ("lockmanager request key",  decode_lockmanager),
    0x6C: ("distribution request",     decode_distribution_request),
    0x82: ("track info long",          decode_track_info_long),
    0x87: ("source status info",       decode_source_status),
    0x96: ("pc present",               decode_pc_present),
    0x98: ("pict/sound status",        decode_pict_sound_status),
}


# ---------------- formatter ----------------

class Ansi:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    GREY    = "\033[90m"


# Short address abbreviations for the compact one-line format.
_ADDR_SHORT = {
    0xC0: "VM",      # Video Master
    0xC1: "AM",      # Audio Master
    0xC2: "SC",      # Source Center
    0x80: "ALL",
    0x81: "AAL",     # All Audio Link
    0x82: "AVL",     # All Video Link
    0x83: "ALL_LK",
    0xF0: "MLGW",
}

# Short telegram-type abbreviations.
_TYPE_SHORT = {
    0x0A: "cmd",
    0x0B: "req",
    0x14: "rsp",
    0x40: "stat",
}


def _addr_short(b: int) -> str:
    return _ADDR_SHORT.get(b, f"0x{b:02x}")


def _type_short(b: int) -> str:
    return _TYPE_SHORT.get(b, f"0x{b:02x}")


def _strip_hex_prefix(v: str) -> str:
    """Decoder values look like '0x7a N.MUSIC' (hex + decoded name). For the
    compact format we just want 'N.MUSIC'. Plain values are returned as-is."""
    if v.startswith("0x") and " " in v:
        return v.split(" ", 1)[1]
    return v


HEADER_FIELDS = [
    ("TO",       lambda t: (t.to_addr,       lookup(ml_src_type_dict,        t.to_addr))),
    ("FROM",     lambda t: (t.from_addr,     lookup(ml_src_type_dict,        t.from_addr))),
    ("TYPE",     lambda t: (t.telegram_type, lookup(ml_telegram_type_dict,   t.telegram_type))),
    ("SRC_DST",  lambda t: (t.src_dest,      lookup(ml_selectedsourcedict,   t.src_dest))),
    ("ORIG_SRC", lambda t: (t.orig_src,      lookup(ml_selectedsourcedict,   t.orig_src))),
    ("PL_TYPE",  lambda t: (t.payload_type,  lookup(ml_command_type_dict,    t.payload_type))),
    ("PL_LEN",   lambda t: (t.payload_len,   str(t.payload_len))),
]


class Formatter:
    """Two output modes: compact (one line per telegram, default) or verbose
    (multi-line with every header field and named payload section)."""

    def __init__(self, color: bool = True, verbose: bool = False,
                 show_hex: bool = False):
        self.color = color and sys.stdout.isatty()
        self.verbose = verbose
        self.show_hex = show_hex

    def _c(self, text: str, code: str) -> str:
        return f"{code}{text}{Ansi.RESET}" if self.color else text

    # ----- compact one-line format -----

    def _format_short(self, t: Telegram) -> str:
        ts = t.timestamp.strftime("%H:%M:%S.%f")[:-3]
        dir_color = Ansi.GREEN if t.direction == "rx" else Ansi.BLUE
        arrow     = "RX" if t.direction == "rx" else "TX"

        fr = _addr_short(t.from_addr)
        to = _addr_short(t.to_addr)
        typ = _type_short(t.telegram_type)
        pl_name = lookup(ml_command_type_dict, t.payload_type)
        if pl_name.startswith("UNKNOWN"):
            pl_name = f"0x{t.payload_type:02x}"

        # decoded payload as a tail string
        decoder = PAYLOAD_DECODERS.get(t.payload_type)
        tail = ""
        if decoder is not None:
            try:
                rows = decoder[1](t)
                tail = " ".join(
                    f"{k.lower()}={_strip_hex_prefix(v).replace(' ', '_')}"
                    for k, v in rows
                    if k != "ERROR"
                )
                err = next((v for k, v in rows if k == "ERROR"), None)
                if err:
                    tail = self._c(f"!{err}", Ansi.RED)
            except IndexError:
                tail = self._c("!short", Ansi.RED)
        elif t.payload_len > 0:
            try:
                tail = self._c(f"data={t.payload.hex()}", Ansi.DIM)
            except IndexError:
                tail = self._c("!short", Ansi.RED)

        parts = [
            self._c(ts, Ansi.GREY),
            self._c(arrow, dir_color + Ansi.BOLD),
            f"{self._c(fr, Ansi.CYAN)}{self._c('→', Ansi.GREY)}{self._c(to, Ansi.CYAN)}",
            self._c(typ, Ansi.YELLOW),
            self._c(pl_name, Ansi.MAGENTA),
        ]
        if tail:
            parts.append(tail)
        line = "  ".join(parts)
        if self.show_hex:
            line += "  " + self._c(t.raw.hex(), Ansi.DIM)
        return line

    # ----- verbose multi-line format -----

    def _format_long(self, t: Telegram) -> str:
        ts = t.timestamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if t.direction == "rx":
            arrow = self._c("RX <--", Ansi.GREEN + Ansi.BOLD)
        else:
            arrow = self._c("TX -->", Ansi.BLUE  + Ansi.BOLD)
        lines = [self._c(f"[{ts}]", Ansi.GREY) + f" {arrow} {t.raw.hex()}"]
        for label, fn in HEADER_FIELDS:
            try:
                val, txt = fn(t)
            except IndexError:
                lines.append(f"  {label:<9} (missing)")
                continue
            lines.append(f"  {label:<9} 0x{val:02x}  {txt}")
        decoder = PAYLOAD_DECODERS.get(t.payload_type)
        if decoder is not None:
            label, fn = decoder
            lines.append("  " + self._c(f"PAYLOAD ({label})", Ansi.YELLOW))
            try:
                rows = fn(t)
            except IndexError as e:
                rows = [("ERROR", f"index {e} -- short telegram?")]
            for k, v in rows:
                lines.append(f"    {k:<13} {v}")
        return "\n".join(lines)

    def format(self, t: Telegram) -> str:
        return self._format_long(t) if self.verbose else self._format_short(t)


# ---------------- listener ----------------

def listen(redis_host: str, redis_port: int,
           channels: List[Tuple[str, str]],
           formatter: Formatter,
           stop: "object_with_is_set") -> None:
    # NB: we deliberately do NOT use pubsub.listen() -- it blocks indefinitely
    # inside the socket read, so SIGINT/SIGTERM (which set the `stop` flag)
    # don't take effect until the next message arrives. Instead we poll with
    # get_message(timeout=...) so the stop flag is checked between polls.
    r = redis.StrictRedis(host=redis_host, port=redis_port, db=0,
                          socket_keepalive=True)
    pubsub: Optional[redis.client.PubSub] = None
    try:
        while not stop.is_set():
            try:
                if pubsub is None:
                    pubsub = r.pubsub()
                    pubsub.subscribe(*[ch for ch, _ in channels])
                m = pubsub.get_message(timeout=0.5,
                                       ignore_subscribe_messages=True)
                if m is None:
                    continue
                ch = m.get("channel")
                if isinstance(ch, bytes):
                    ch = ch.decode("utf-8", errors="replace")
                direction = next((d for c, d in channels if c == ch), "?")
                data = m.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                try:
                    t = Telegram.from_hex(data, direction)
                except ValueError as e:
                    print(f"[ml-debug] bad hex on {ch}: {data!r} -- {e}",
                          file=sys.stderr)
                    continue
                print(formatter.format(t), flush=True)
            except redis.exceptions.RedisError as e:
                print(f"[ml-debug] redis error: {e}", file=sys.stderr)
                if pubsub is not None:
                    try: pubsub.close()
                    except Exception: pass
                    pubsub = None
                # short sleep, but interruptible by stop flag
                stop.wait(1.0)
    finally:
        if pubsub is not None:
            try: pubsub.close()
            except Exception: pass
        try: r.close()
        except Exception: pass


# ---------------- CLI ----------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--rx-only", action="store_true",
                     help="only show telegrams from link:ml:receive")
    grp.add_argument("--tx-only", action="store_true",
                     help="only show telegrams from link:ml:transmit")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="multi-line output with every header field decoded")
    ap.add_argument("--hex", action="store_true",
                    help="append the raw hex bytes to each compact line")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color (auto-disabled if stdout isn't a TTY)")
    args = ap.parse_args()

    channels: List[Tuple[str, str]] = []
    if not args.tx_only:
        channels.append(("link:ml:receive",  "rx"))
    if not args.rx_only:
        channels.append(("link:ml:transmit", "tx"))

    formatter = Formatter(color=not args.no_color,
                          verbose=args.verbose,
                          show_hex=args.hex)

    import threading
    stop = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    listen(args.redis_host, args.redis_port, channels, formatter, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
