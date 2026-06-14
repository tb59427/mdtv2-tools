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
REDIS_ML_TX    = "link:ml:transmit"
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
PT_STANDBY          = 0x10
PT_RELEASE          = 0x11
PT_VIRTUAL_BEO4     = 0x20      # MLGW_REMOTE_BEO4 (virtual keypress)
KEY_STANDBY         = 0x0C      # Beo4 STANDBY
ADDR_MLGW           = 0xF0      # virtual keys to MLGW are home-automation,
                                # not transport -- ignore those here

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
                    "track": None}

        if pt == PT_VIRTUAL_BEO4 and to != ADDR_MLGW:
            # A virtual Beo4 keypress. We only care about STANDBY here --
            # it's the clearest "system going off" signal (captured off
            # sequence: virtual_beo4 0x0C, then a RELEASE flurry). The
            # key sits just past the 5-byte payload, at raw[14].
            if len(raw) > 14 and raw[14] == KEY_STANDBY:
                return {"from": frm, "source": None,
                        "activity": 0x06, "track": None}   # Standby
            return None
        return None
    except Exception:
        return None


class MLState:
    def __init__(self) -> None:
        self.source: Optional[int] = None
        self.activity: Optional[int] = None
        self.track = None
        self.frm: Optional[int] = None
        self.origin: Optional[str] = None

    def update(self, raw: bytes, origin: str) -> bool:
        d = parse_ml(raw)
        if d is None:
            return False
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


def listen(redis_host: str, redis_port: int, stop: threading.Event) -> None:
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
    args = ap.parse_args()

    stop = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    listen(args.redis_host, args.redis_port, stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
