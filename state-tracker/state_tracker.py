#!/usr/bin/env python3
"""state_tracker.py -- maintain current bus state as Redis variables.

A small daemon that snoops the broker's ML / DL'80 / DL'86 Redis channels
(both receive and transmit) and keeps a decoded "what is the system doing
right now" view in three Redis keys:

    state:ml      JSON: active ML source + activity + track
    state:dl80    JSON: Beogram/Tape transport state (DL'80)
    state:dl86    JSON: integrated-system source + transport + track + volume

On every change it also PUBLISHes the same JSON blob to a per-bus event
channel so reactive consumers don't have to poll:

    link:ml:state    link:dl80:state    link:dl86:state

This daemon is read-only with respect to the bus: it only consumes the
channels the broker already populates. The broker stays a pure byte-pipe
and is untouched. The decode tables here are intentionally a self-
contained copy of the ones in ml-debug / dl-debug (same convention the
repo already uses -- each tool carries its own copy).

Run as a systemd service (mdt-state.service) or by hand:
    python3 state_tracker.py [--redis-host H] [--redis-port P]
"""
from __future__ import annotations

import argparse
import datetime
import json
import signal
import sys
import threading
from typing import Optional

import redis


# ============================================================================
# logging  (mirror broker/dl-debug: stdout for journald + shared /tmp/mdt.log)
# ============================================================================

_LOG_FILE = "/tmp/mdt.log"
_log_lock = threading.Lock()


def log(msg: str, *, err: bool = False) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    level = "WARN " if err else "INFO "
    line = f"{ts} {level} [state] {msg}"
    print(line, file=sys.stderr if err else sys.stdout, flush=True)
    try:
        with _log_lock:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _hexb(v: Optional[int]) -> Optional[str]:
    return None if v is None else f"0x{v:02x}"


# ============================================================================
# Redis channels + keys
# ============================================================================

REDIS_ML_RX    = "link:ml:receive"
REDIS_ML_TX    = "link:ml:transmit"   # also where we publish the source query
REDIS_DL80_RX  = "link:dl80:receive"
REDIS_DL80_TX  = "link:dl80:transmit"
REDIS_DL86_RX  = "link:dl86:receive"
REDIS_DL86_TX  = "link:dl86:transmit"

STATE_KEY   = {"ml": "state:ml",    "dl80": "state:dl80",  "dl86": "state:dl86"}
EVENT_CHAN  = {"ml": "link:ml:state", "dl80": "link:dl80:state",
               "dl86": "link:dl86:state"}

# (channel, bus, origin)
CHANNELS = [
    (REDIS_ML_RX,   "ml",   "rx"),
    (REDIS_ML_TX,   "ml",   "tx"),
    (REDIS_DL80_RX, "dl80", "rx"),
    (REDIS_DL80_TX, "dl80", "tx"),
    (REDIS_DL86_RX, "dl86", "rx"),
    (REDIS_DL86_TX, "dl86", "tx"),
]


# ============================================================================
# ML decode  (copy of ml-debug/const.py + ml-debug/ml_debug.py offsets)
# ============================================================================

PT_STATUS_INFO      = 0x87
PT_TRACK_INFO_LONG  = 0x82
PT_TRACK_INFO       = 0x44      # TRACK_INFO; kind 0x05 = CURRENT_SOURCE
PT_GOTO_SOURCE      = 0x45
PT_REQ_DIST_SOURCE  = 0x08      # REQUEST_DISTRIBUTED_SOURCE
PT_STANDBY          = 0x10
PT_RELEASE          = 0x11
PT_VIRTUAL_BEO4     = 0x20      # MLGW_REMOTE_BEO4 (virtual keypress)
TT_RESPONSE         = 0x14
TT_REQUEST          = 0x0B
KEY_STANDBY         = 0x0C      # Beo4 STANDBY
ADDR_MLGW           = 0xF0      # virtual keys to MLGW are home-automation,
                                # not transport -- ignore those here
ADDR_AM             = 0xC1
ADDR_VM             = 0xC0

# Source-query telegram. Emulates a link room speaker asking a master
# "what source are you distributing?" -- the only NON-DISRUPTIVE way to
# read the current source on demand (verified: CD/radio kept playing).
# A master answers a *link device* (not the other master), so FROM must
# be a link-room address (default 0x06, as captured). We query BOTH
# masters at startup:
#   - AM (0xC1) answers with the distributed AUDIO source byte at raw[13]
#     (RESPONSE 0x14, pl_len>=5):
#       TX  c1 06 01 0b 00 00 00 08 00 01
#       RX  06 c1 01 14 00 00 00 08 05 06 02 01 00 <SRC> 01
#   - VM (0xC0) answers pl_len=0 (a bare ack, no source) on this system,
#     but we query it too so the daemon also works in VM-led / AM-absent
#     topologies where the VM may carry the answer.
DEFAULT_QUERY_ADDR  = 0x06
QUERY_MASTERS       = (ADDR_AM, ADDR_VM)

# Source-byte -> which master's domain it belongs to. ML has two
# independent masters: the Audio Master (audio path) and the Video
# Master (video path). We route each source-carrying telegram into the
# matching slot by the *source category*, not by the FROM address --
# that way an audio source announced by the Source Center (0xc2, e.g.
# our own AirPlay N.RADIO) still lands in the "am" view without giving
# the non-master SC its own slot.
SOURCE_KIND = {
    # audio domain -> am
    0x47: "am",  # PC
    0x6f: "am",  # RADIO
    0x79: "am",  # A.MEM
    0x7a: "am",  # N.MUSIC
    0x8d: "am",  # CD
    0x97: "am",  # A.AUX
    0xa1: "am",  # N.RADIO
    # video domain -> vm
    0x0b: "vm",  # TV
    0x15: "vm",  # V.MEM
    0x16: "vm",  # DVD2
    0x1f: "vm",  # DTV
    0x29: "vm",  # DVD
    0x33: "vm",  # V.AUX
    0x3e: "vm",  # DOORCAM
}

# Audio sources we'll "join" with a GOTO to fetch the track (see
# _build_goto). Gating to these means we never accidentally GOTO a
# video source or a junk byte.
AUDIO_SOURCES = {s for s, kind in SOURCE_KIND.items() if kind == "am"}


def _build_source_query(link_addr: int, master: int) -> str:
    # body only; the broker appends checksum + EOL.
    return bytes([master, link_addr, 0x01, TT_REQUEST,
                  0x00, 0x00, 0x00, PT_REQ_DIST_SOURCE, 0x00, 0x01]).hex()


def _build_goto(link_addr: int, source: int) -> str:
    """GOTO_SOURCE for the CURRENT source -- a link-join/re-announce, NOT
    a source switch (so it's non-disruptive). Makes the AM re-broadcast
    STATUS_INFO with CH_TRACK. Captured form:
        c1 06 01 0b 00 00 00 45 07 01 02 <SRC> 00 02 01 00
    """
    return bytes([ADDR_AM, link_addr, 0x01, TT_REQUEST,
                  0x00, 0x00, 0x00, PT_GOTO_SOURCE, 0x07,
                  0x01, 0x02, source, 0x00, 0x02, 0x01, 0x00]).hex()

ML_SOURCE_NAMES = {
    0x00: "NONE", 0x0B: "TV", 0x15: "V.MEM", 0x16: "DVD2", 0x1F: "DTV",
    0x29: "DVD", 0x33: "V.AUX", 0x3E: "DOORCAM", 0x47: "PC", 0x6F: "RADIO",
    0x79: "A.MEM", 0x7A: "N.MUSIC", 0x8D: "CD", 0x97: "A.AUX", 0xA1: "N.RADIO",
    0xFE: "<ALL>",
}

ML_ACTIVITY_NAMES = {
    0x00: "Unknown", 0x01: "Stop", 0x02: "Playing", 0x03: "Fast Forward",
    0x04: "Rewind", 0x05: "Record Lock", 0x06: "Standby", 0x07: "Load/No Media",
    0x08: "Still Picture", 0x14: "Scan Forward", 0x15: "Scan Reverse",
    0xFF: "Blank Status",
}


def parse_ml(raw: bytes) -> Optional[dict]:
    """Extract {from, source, activity, track} from the telegram types that
    carry current-source state. Returns None for everything else (and for
    malformed/short telegrams -- never throws). Offsets are absolute within
    the telegram (header[0..8], payload at [9]); they hold for both RX hex
    (full telegram incl. checksum+EOL) and TX hex (no trailing bytes)
    because we index from the front.

    Robustness: STATUS_INFO / TRACK_INFO_LONG are accepted only when the
    source byte is a *known* ML source. The bus carries short stub
    STATUS_INFO frames (pl_len=0, e.g. the VM's `...870004e3` where the
    `e3` is the checksum, not a source) and VM frames advertising
    non-audio/transient bytes; both would otherwise clobber a good
    source with garbage like 0xe3 -> "?". Gating on the known-source set
    drops them cleanly."""
    try:
        if len(raw) < 9:
            return None
        pt = raw[7]
        pl = raw[8]
        frm = raw[1]
        to = raw[0]

        if pt == PT_STATUS_INFO:
            # Need the full status payload for source(@10) + activity(@21)
            # to be real, not checksum/EOL bytes of a short stub frame.
            if len(raw) < 22 or pl < 0x0D:
                return None
            source = raw[10]
            if source not in ML_SOURCE_NAMES:
                return None                    # unknown/transient -> ignore
            activity = raw[21]
            if pl < 27:
                track = raw[19] if len(raw) > 19 else None
            elif len(raw) > 37:
                track = raw[36] * 256 + raw[37]
            else:
                track = None
            return {"from": frm, "source": source,
                    "activity": activity, "track": track}

        if pt == PT_TRACK_INFO_LONG:
            if len(raw) < 14:
                return None
            if raw[11] not in ML_SOURCE_NAMES:
                return None
            return {"from": frm, "source": raw[11],
                    "track": raw[12], "activity": raw[13]}

        if pt in (PT_STANDBY, PT_RELEASE):
            src = raw[4]                       # src_dest = source going idle
            return {"from": frm,
                    "source": src if src in ML_SOURCE_NAMES else None,
                    "activity": 0x06 if pt == PT_STANDBY else 0x01,
                    "track": None,
                    "standby": True}           # source-less form -> both slots

        if pt == PT_REQ_DIST_SOURCE and raw[3] == TT_RESPONSE:
            # Reply to a distributed-source query (ours, or a real link
            # speaker joining). Source byte at raw[13]. A distributed
            # source is by definition active -> mark Playing.
            if pl < 5 or len(raw) < 14:
                return None
            src = raw[13]
            if src not in ML_SOURCE_NAMES or src == 0x00:
                return None
            return {"from": frm, "source": src,
                    "activity": 0x02, "track": None}

        if pt == PT_TRACK_INFO and pl >= 0x0b and len(raw) >= 12:
            # TRACK_INFO kind (raw[9]) 0x05 = CURRENT_SOURCE: the AM's
            # authoritative "the current source is X", source at raw[11].
            if raw[9] == 0x05 and raw[11] in ML_SOURCE_NAMES:
                return {"from": frm, "source": raw[11],
                        "activity": 0x02, "track": None}
            return None

        if pt == PT_VIRTUAL_BEO4 and to != ADDR_MLGW:
            # A virtual Beo4 keypress. We only care about STANDBY here --
            # it's the clearest "system going off" signal (captured off
            # sequence: virtual_beo4 0x0C, then a RELEASE flurry). The
            # key sits just past the 5-byte payload, at raw[14].
            if len(raw) > 14 and raw[14] == KEY_STANDBY:
                return {"from": frm, "source": None,
                        "activity": 0x06, "track": None,
                        "standby": True}                   # Standby
            return None
        return None
    except Exception:
        return None


class _MasterState:
    """One master's view -- am (audio path) or vm (video path)."""

    def __init__(self) -> None:
        self.source: Optional[int] = None
        self.activity: Optional[int] = None
        self.track = None
        self.frm: Optional[int] = None
        self.origin: Optional[str] = None

    def apply(self, d: dict, origin: str) -> bool:
        """Overlay the parsed fields; returns True if the published view
        (source/activity/track) changed."""
        before = (self.source, self.activity, self.track)
        if d.get("source") is not None:
            self.source = d["source"]
        if d.get("activity") is not None:
            self.activity = d["activity"]
        if "track" in d:
            self.track = d["track"]
        self.frm = d.get("from")
        self.origin = origin
        return (self.source, self.activity, self.track) != before

    def as_blob(self) -> dict:
        return {
            "source": _hexb(self.source),
            "source_name": (ML_SOURCE_NAMES.get(self.source, "?")
                            if self.source is not None else None),
            "activity": _hexb(self.activity),
            "activity_name": (ML_ACTIVITY_NAMES.get(self.activity, "?")
                              if self.activity is not None else None),
            "playing": self.activity == 0x02,
            "track": self.track,
            "from": _hexb(self.frm),
            "origin": self.origin,
        }


class MLState:
    """The ML view, split by master: am (audio) and vm (video). Each
    source-carrying telegram is routed to a slot by source category
    (SOURCE_KIND), so the two never clobber each other -- a video
    STATUS_INFO can't overwrite the active audio source.

    Standby/release is only acted on when it NAMES a source (then it
    idles that source's slot). A source-less standby is deliberately
    ignored: it's ambiguous and does NOT imply audio stopped. E.g. when
    the VM switches its own screen to a video source it sends a
    source-less STANDDBY to the AM, but the AM keeps distributing the
    audio source to other zones (a link room) -- idling `am` there would
    be wrong. A real power-off instead sends per-source RELEASE
    telegrams (which carry a source) and those idle the right slots."""

    def __init__(self) -> None:
        self.am = _MasterState()
        self.vm = _MasterState()

    def update(self, raw: bytes, origin: str) -> bool:
        d = parse_ml(raw)
        if d is None:
            return False
        src = d.get("source")
        if src is not None and src in SOURCE_KIND:
            slot = self.am if SOURCE_KIND[src] == "am" else self.vm
            return slot.apply(d, origin)
        return False                    # source-less standby/etc: ignore

    def as_blob(self) -> dict:
        return {
            "am": self.am.as_blob(),
            "vm": self.vm.as_blob(),
            "updated": _now_iso(),
        }


# ============================================================================
# DL'80 decode  (copy of dl-debug/dl_debug.py DL80_OPCODES)
# ============================================================================

DL80_OPCODES = {
    0x00: ("", "EOM"), 0x80: ("NOINPUT", "TP.NoRecInput"),
    0x95: ("PHNEXT", "BG.ADV->"), 0x99: ("TPNEXT", "TP.ADV->"),
    0x9A: ("PHSTNDBY", "BG.Standby"), 0x9B: ("TPSTNDBY", "TP.Standby"),
    0xA9: ("PHPLY", "Sys.Src=>BG.Play"), 0xAB: ("TPPLY", "Sys.Src=>TP.Play"),
    0xAD: ("REC", "TP.Record"), 0xAF: ("TPREW", "TP.<<-RW"),
    0xB1: ("TPFF", "TP.FF->>"), 0xB3: ("TPRETURN", "TP.<-Ret"),
    0xB4: ("STPSTOP", "TP.Status.Stopped"), 0xB5: ("TPSTOP", "TP.Stop"),
    0xB7: ("STPON", "TP.Status.Playing"), 0xC1: ("STPOFF", "TP.Status.Standby"),
    0xC3: ("SPHON", "BG.Status.Playing"), 0xC4: ("PHSTEPAD", "BG.Status.StepAdv->"),
    0xC5: ("SPHOFF", "BG.Status.Standby"), 0xC6: ("SNOTAPE", "TP.Status.NoSource"),
    0xC8: ("SNODISC", "BG.Status.NoMedia"), 0xC9: ("PHEND", "BG.Pause"),
    0xCB: ("OFFSYSTM", "Sys.Standby"), 0xCD: ("PHPSE", "BG.Pause"),
    0xCE: ("SPHSTOP", "BG.Status.Stopped"), 0xCF: ("PHSTOP", "BG.Stop"),
    0xD2: ("SPHMOOUT", "BG.Status.FF->>"), 0xD3: ("PHMOOUT", "BG.FF->>"),
    0xD5: ("PHST", "BG.Start"),
    0xF0: ("F0DATA5N", "BG.Status.TrackNum"), 0xF2: ("F2DATA5N", "BG.Status.Counter"),
    0xF4: ("F4DATA5N", "TP.Status.TrackNum"), 0xFC: ("RELEASE", "Sys.Release"),
    0xFD: ("WAIT", "BG.Status.Load"),
}

# Status opcodes -> (device, transport). These are the single-byte status
# announcements the components emit; they are idempotent so the DL'80
# on-wire doubling (each byte sent twice) is harmless -- the second copy
# produces no state change.
DL80_STATUS = {
    0xC3: ("BG", "playing"),  0xCE: ("BG", "stopped"),  0xC5: ("BG", "standby"),
    0xC8: ("BG", "no-media"), 0xC4: ("BG", "step-adv"), 0xD2: ("BG", "ff"),
    0xFD: ("BG", "load"),
    0xB7: ("TP", "playing"),  0xB4: ("TP", "stopped"),  0xC1: ("TP", "standby"),
    0xC6: ("TP", "no-source"),
    0xCB: ("SYS", "standby"),
}


class DL80State:
    """Reconstruct device + transport from the single-byte status stream.

    Numeric track is NOT decoded: DL'80 track number arrives as the
    multi-byte F0/F4 (BG/TP TrackNum) families whose 40-bit arg layout
    we have not verified, so we expose track=null rather than guess.
    Device + transport (from the verified status opcodes) is reliable.
    """
    def __init__(self) -> None:
        self.device: Optional[str] = None
        self.transport: Optional[str] = None
        self.last_op: Optional[int] = None
        self.origin: Optional[str] = None

    def update(self, byte: int, origin: str) -> bool:
        info = DL80_STATUS.get(byte)
        if info is None:
            return False                      # not a status opcode we track
        dev, tr = info
        before = (self.device, self.transport)
        if dev == "SYS":
            self.transport = "standby"        # whole-system; keep device
        else:
            self.device, self.transport = dev, tr
        self.last_op = byte
        self.origin = origin
        return (self.device, self.transport) != before

    def as_blob(self) -> dict:
        name = (DL80_OPCODES.get(self.last_op, ("", ""))[1]
                if self.last_op is not None else None)
        return {
            "device": self.device,
            "transport": self.transport,
            "track": None,                    # see class docstring
            "last_opcode": _hexb(self.last_op),
            "last_opcode_name": name,
            "origin": self.origin,
            "updated": _now_iso(),
        }


# ============================================================================
# DL'86 decode  (copy of dl-debug/dl_debug.py status-frame logic)
# ============================================================================

DL86_ADDRESSES = {
    0b00001: "MCL", 0b01111: "Radio (default)", 0b10010: "CD",
    0b11110: "Radio / BM",
}
DL86_STATUS_SUBTYPES = {
    0b0100: "Status", 0b0110: "RecStatus", 0b1100: "Status?", 0b0000: "Idle?",
}


def _hex_to_bits(bit_count: int, hex_str: str) -> str:
    raw = bytes.fromhex(hex_str)
    bits = "".join(f"{b:08b}" for b in raw)
    return bits[:bit_count]


def decode_dl86_status(bit_count: int, hex_str: str) -> Optional[dict]:
    """Decode a 40-bit DL'86 STATUS frame into structured fields. Returns
    None for non-status / non-40-bit frames. Never throws.

    Status payload semantics (verified on a real B&O music system):
        from=BM, sub=Status:
          mod 0x0  d1=0x00 -> playing, track=d2 (1-indexed)
                   d1=0x30 -> lid-open/loading
                   d1=0x40 -> stopped
                   to=MCL d1=0x02 d2=0xFF -> Sys.Standby
          mod 0x8  volume=d2  (d1 redundant: 2*d2+0x40)
    """
    try:
        if bit_count != 40:
            return None
        bits = _hex_to_bits(bit_count, hex_str)
        if len(bits) < 40 or bits[:6] != "001110":
            return None
        frm = int(bits[6:11], 2)
        to  = int(bits[11:16], 2)
        sub = int(bits[16:20], 2)
        mod = int(bits[20:24], 2)
        d1  = int(bits[24:32], 2)
        d2  = int(bits[32:40], 2)
        out = {
            "from": frm, "to": to,
            "from_name": DL86_ADDRESSES.get(frm, f"0x{frm:02x}"),
            "to_name": DL86_ADDRESSES.get(to, f"0x{to:02x}"),
            "sub": sub, "mod": mod, "d1": d1, "d2": d2,
        }
        if frm == 0b11110 and sub == 0b0100:
            if mod == 0x0:
                if to == 0b00001 and d1 == 0x02 and d2 == 0xFF:
                    out["standby"] = True
                elif d1 == 0x00:
                    out["transport"] = "playing"
                    out["track"] = d2 or None
                elif d1 == 0x30:
                    out["transport"] = "lid-open/loading"
                    out["track"] = None
                elif d1 == 0x40:
                    out["transport"] = "stopped"
                    out["track"] = None
                else:
                    out["transport"] = f"state-0x{d1:02x}"
            elif mod == 0x8:
                out["volume"] = d2
        return out
    except Exception:
        return None


class DL86State:
    def __init__(self) -> None:
        self.to: Optional[int] = None
        self.to_name: Optional[str] = None
        self.frm_name: Optional[str] = None
        self.transport: Optional[str] = None
        self.track = None
        self.volume = None
        self.origin: Optional[str] = None

    def update(self, bit_count: int, hex_str: str, origin: str) -> bool:
        d = decode_dl86_status(bit_count, hex_str)
        if d is None:
            return False
        before = (self.to, self.transport, self.track, self.volume)
        self.to = d["to"]
        self.to_name = d["to_name"]
        self.frm_name = d["from_name"]
        self.origin = origin
        if d.get("standby"):
            self.transport = "standby"
        else:
            if d.get("transport") is not None:
                self.transport = d["transport"]
            if "track" in d:
                self.track = d["track"]
            if "volume" in d:
                self.volume = d["volume"]
        return (self.to, self.transport, self.track, self.volume) != before

    def as_blob(self) -> dict:
        return {
            "to": _hexb(self.to),
            "to_name": self.to_name,
            "from_name": self.frm_name,
            "transport": self.transport,
            "track": self.track,
            "volume": self.volume,
            "origin": self.origin,
            "updated": _now_iso(),
        }


# ============================================================================
# listen loop
# ============================================================================

def _publish(r: "redis.StrictRedis", bus: str, blob: dict) -> None:
    s = json.dumps(blob)
    r.set(STATE_KEY[bus], s)
    r.publish(EVENT_CHAN[bus], s)


def _startup_query(r: "redis.StrictRedis", ml: "MLState",
                   stop: threading.Event, link_addr: int,
                   do_goto: bool = True,
                   attempts: int = 5, interval: float = 3.0) -> None:
    """Populate state:ml at startup by emulating a link-room speaker.

    Phase 1 -- learn the source: ask both masters (AM 0xC1, VM 0xC0)
    REQUEST_DISTRIBUTED_SOURCE. Non-disruptive read.

    Phase 2 (if do_goto) -- fetch the track: once we know an audio
    source but not its track, send GOTO_SOURCE for that SAME source.
    That's a link-join/re-announce (not a switch), so it's
    non-disruptive, and it makes the AM broadcast a full STATUS_INFO
    with CH_TRACK which the listen loop then parses.
    """
    # The query/GOTO concern the audio path, so we track the am slot.
    def _wait_source(timeout: float) -> None:
        # Poll in small steps so we proceed the instant the reply lands.
        steps = max(1, int(timeout / 0.2))
        for _ in range(steps):
            if stop.is_set() or ml.am.source is not None:
                return
            stop.wait(0.2)

    queries = [(_build_source_query(link_addr, m), m) for m in QUERY_MASTERS]
    # Phase 1: learn the source.
    for i in range(attempts):
        if stop.is_set() or ml.am.source is not None:
            break
        for q, master in queries:
            try:
                r.publish(REDIS_ML_TX, q)
                log(f"source query -> 0x{master:02x} (as link "
                    f"0x{link_addr:02x}), attempt {i + 1}/{attempts}")
            except redis.exceptions.RedisError:
                pass
            stop.wait(0.3)        # small gap between the two masters
        _wait_source(interval)    # break out the moment a reply arrives

    # Phase 2: fetch the track via a join/re-announce of the current
    # source. Only for known audio sources, and only if we don't already
    # have a track (a spontaneous STATUS_INFO may have supplied it).
    if (do_goto and not stop.is_set()
            and ml.am.source in AUDIO_SOURCES and ml.am.track is None):
        src = ml.am.source
        try:
            r.publish(REDIS_ML_TX, _build_goto(link_addr, src))
            log(f"goto-refresh -> AM (join current source "
                f"0x{src:02x} as link 0x{link_addr:02x}) to fetch track")
        except redis.exceptions.RedisError:
            pass


def listen(redis_host: str, redis_port: int, stop: threading.Event,
           query_addr: Optional[int] = DEFAULT_QUERY_ADDR,
           do_goto: bool = True) -> None:
    r = redis.StrictRedis(host=redis_host, port=redis_port, db=0,
                          socket_keepalive=True)
    ml, dl80, dl86 = MLState(), DL80State(), DL86State()
    trackers = {"ml": ml, "dl80": dl80, "dl86": dl86}

    # Seed all three keys so consumers always have something to GET.
    for bus, t in trackers.items():
        try:
            _publish(r, bus, t.as_blob())
        except redis.exceptions.RedisError:
            pass
    log(f"state tracker up; keys: {', '.join(STATE_KEY.values())}")

    # Kick off the non-disruptive source query (unless disabled). Runs in
    # a daemon thread so the listen loop is already receiving when the
    # AM's reply arrives.
    if query_addr is not None:
        threading.Thread(
            target=_startup_query, args=(r, ml, stop, query_addr, do_goto),
            name="startup-query", daemon=True).start()

    pubsub: Optional[redis.client.PubSub] = None
    try:
        while not stop.is_set():
            try:
                if pubsub is None:
                    pubsub = r.pubsub()
                    pubsub.subscribe(*[ch for ch, _, _ in CHANNELS])
                m = pubsub.get_message(timeout=0.5,
                                       ignore_subscribe_messages=True)
                if m is None:
                    continue
                ch = m.get("channel")
                if isinstance(ch, bytes):
                    ch = ch.decode("utf-8", errors="replace")
                bus, origin = next(((b, o) for c, b, o in CHANNELS if c == ch),
                                   (None, None))
                if bus is None:
                    continue
                data = m.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                data = data.strip()

                changed = False
                if bus == "ml":
                    try:
                        raw = bytes.fromhex(data)
                    except ValueError:
                        continue
                    changed = ml.update(raw, origin)
                elif bus == "dl80":
                    try:
                        b = int(data, 16) & 0xFF
                    except ValueError:
                        continue
                    changed = dl80.update(b, origin)
                elif bus == "dl86":
                    if ":" in data:
                        a, _, hx = data.partition(":")
                        try:
                            bit_count = int(a)
                        except ValueError:
                            continue
                        hx = hx.strip()
                    else:
                        hx = data
                        try:
                            bit_count = 8 * len(bytes.fromhex(hx))
                        except ValueError:
                            continue
                    changed = dl86.update(bit_count, hx, origin)

                if changed:
                    blob = trackers[bus].as_blob()
                    _publish(r, bus, blob)
                    log(f"{bus.upper()} STATE: "
                        + json.dumps({k: v for k, v in blob.items()
                                      if k != "updated"}))
            except redis.exceptions.RedisError as e:
                log(f"redis error: {e}", err=True)
                if pubsub is not None:
                    try: pubsub.close()
                    except Exception: pass
                    pubsub = None
                stop.wait(1.0)
    finally:
        if pubsub is not None:
            try: pubsub.close()
            except Exception: pass
        try: r.close()
        except Exception: pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)
    ap.add_argument("--query-addr", default=hex(DEFAULT_QUERY_ADDR),
                    help="link-room address to emulate for the startup "
                         "source query (default 0x06, as captured). Must be "
                         "a link device the AM will answer -- NOT a master.")
    ap.add_argument("--no-query", action="store_true",
                    help="disable the startup source query (purely passive; "
                         "use this if a real link speaker occupies the query "
                         "address)")
    ap.add_argument("--no-goto", action="store_true",
                    help="skip the startup GOTO-refresh that fetches the "
                         "track (keeps the query read-only: source only, no "
                         "phantom link-join). Track then comes from the next "
                         "spontaneous broadcast.")
    args = ap.parse_args()

    query_addr = None
    if not args.no_query:
        try:
            query_addr = int(args.query_addr, 0) & 0xFF
        except ValueError:
            print(f"[state] bad --query-addr {args.query_addr!r}", file=sys.stderr)
            return 2

    stop = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    listen(args.redis_host, args.redis_port, stop, query_addr=query_addr,
           do_goto=not args.no_goto)
    return 0


if __name__ == "__main__":
    sys.exit(main())
