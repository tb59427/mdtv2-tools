#!/usr/bin/env python3
"""dl-debug -- decode Datalink '80 and Datalink '86 messages from the
broker into human-readable lines.

Subscribes to `link:dl80:transmit`, `link:dl80:receive`,
`link:dl86:transmit`, and `link:dl86:receive` and prints one line per
message.

Usage:
    ./dl_debug.py                       # both protocols, both directions
    ./dl_debug.py --rx-only             # only receive side
    ./dl_debug.py --dl80-only -v        # only DL'80, verbose
    ./dl_debug.py --no-dedup-dl80       # show both copies of DL'80 doublings

Pairs with mdtv2-broker.py.
"""
from __future__ import annotations

import argparse
import datetime
import signal
import sys
import threading
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

import redis


# ============================================================================
# DL'80 decode tables (extracted from "Datalink 80 Protocol.xlsx", Protocol
# sheet). Keys are the byte values; values are (mnemonic, descriptive_name).
# ============================================================================

DL80_OPCODES: dict[int, tuple[str, str]] = {
    0x00: ("",          "EOM"),
    0x07: ("",          "14+"),
    0x0F: ("",          "Number 14"),
    0x17: ("",          "Number 13"),
    0x1F: ("",          "Number 12"),
    0x27: ("",          "Number 11"),
    0x2F: ("",          "Number 10"),
    0x37: ("",          "Number 9"),
    0x3F: ("",          "Number 8"),
    0x47: ("",          "Number 7"),
    0x4F: ("",          "Number 6"),
    0x57: ("",          "Number 5"),
    0x5F: ("",          "Number 4"),
    0x67: ("",          "Number 3"),
    0x6F: ("",          "Number 2"),
    0x77: ("",          "Number 1"),
    0x7F: ("",          "Number 0"),
    0x80: ("NOINPUT",   "TP.NoRecInput"),
    0x81: ("NUMERIC0",  "KP.0"),
    0x83: ("NUMERIC1",  "KP.1"),
    0x85: ("NUMERIC2",  "KP.2"),
    0x87: ("NUMERIC3",  "KP.3"),
    0x89: ("NUMERIC4",  "KP.4"),
    0x8B: ("NUMERIC5",  "KP.5"),
    0x8D: ("NUMERIC6",  "KP.6"),
    0x8F: ("NUMERIC7",  "KP.7"),
    0x90: ("NUMERIC7_1", "Side-Bot"),
    0x91: ("NUMERIC8",  "KP.8"),
    0x93: ("NUMERIC9",  "KP.9"),
    0x95: ("PHNEXT",    "BG.ADV->"),
    0x99: ("TPNEXT",    "TP.ADV->"),
    0x9A: ("PHSTNDBY",  "BG.Standby"),
    0x9B: ("TPSTNDBY",  "TP.Standby"),
    0xA8: ("VOLUP_1",   "TP.Status.<-RecRet"),
    0xA9: ("PHPLY",     "Sys.Src=>BG.Play"),
    0xAA: ("SPTPPLY",   "TP.Status.RecReturned"),
    0xAB: ("TPPLY",     "Sys.Src=>TP.Play"),
    0xAC: ("SRECPSE",   "TP.Status.RecPause"),
    0xAD: ("REC",       "TP.Record"),
    0xAE: ("STPREW",    "TP.Status.Step-Done"),
    0xAF: ("TPREW",     "TP.<<-RW"),
    0xB0: ("STPFF",     "TP.Status.Adv->"),
    0xB1: ("TPFF",      "TP.FF->>"),
    0xB2: ("TPTURN",    "TP.Turn"),
    0xB3: ("TPRETURN",  "TP.<-Ret"),
    0xB4: ("STPSTOP",   "TP.Status.Stopped"),
    0xB5: ("TPSTOP",    "TP.Stop"),
    0xB6: ("TPDOLBY",   "TP.Dolby-Select"),
    0xB7: ("STPON",     "TP.Status.Playing"),
    0xB8: ("TPAUTREV",  "TP.AutoRev-Select"),
    0xB9: ("SRECST",    "TP.Status.RecStart"),
    0xBB: ("SRECEND",   "TP.Status.RecEnd"),
    0xBC: ("TPOPEN",    "TP.NewMedia"),
    0xBE: ("TPSTAT",    "TP.ShowStatus"),
    0xC0: ("TPDISPL",   "TP.Display-Select"),
    0xC1: ("STPOFF",    "TP.Status.Standby"),
    0xC2: ("TPGOTO",    "TP.GoTo"),
    0xC3: ("SPHON",     "BG.Status.Playing"),
    0xC4: ("PHSTEPAD",  "BG.Status.StepAdv->"),
    0xC5: ("SPHOFF",    "BG.Status.Standby"),
    0xC6: ("SNOTAPE",   "TP.Status.NoSource"),
    0xC7: ("TPEND",     "TP.Pause"),
    0xC8: ("SNODISC",   "BG.Status.NoMedia"),
    0xC9: ("PHEND",     "BG.Pause"),
    0xCA: ("PHSTEPRT",  "BG.<-StepRet"),
    0xCB: ("OFFSYSTM",  "Sys.Standby"),
    0xCC: ("PHOPEN",    "BG.Track?Numbegin?"),
    0xCD: ("PHPSE",     "BG.Pause"),
    0xCE: ("SPHSTOP",   "BG.Status.Stopped"),
    0xCF: ("PHSTOP",    "BG.Stop"),
    0xD0: ("SPHMOIN",   "TP.AutoRev?RW?"),
    0xD1: ("PHMOIN",    "BG.<<-RW"),
    0xD2: ("SPHMOOUT",  "BG.Status.FF->>"),
    0xD3: ("PHMOOUT",   "BG.FF->>"),
    0xD4: ("PHSTAT",    "BG.ShowStatus"),
    0xD5: ("PHST",      "BG.Start"),
    0xD6: ("PHINDEX",   "BG-Goto"),
    0xD8: ("PHDISPL",   "BG.Display-Select"),
    0xE0: ("PHTURN",    "BG.Turn"),
    0xE4: ("PPMUPDA",   "TP.RecInfo"),
    0xEF: ("REQSTAT",   "Sys.ShowStatus"),
    0xF0: ("F0DATA5N",  "BG.Status.TrackNum (multi-byte: 5 args)"),
    0xF1: ("SRECIMPS",  "BG.Status.RecInput?"),
    0xF2: ("F2DATA5N",  "BG.Status.Counter (multi-byte: 5 args)"),
    0xF3: ("PHRETURN",  "BG.<-Ret"),
    0xF4: ("F4DATA5N",  "TP.Status.TrackNum (multi-byte: 5 args)"),
    0xF5: ("TPTRKSEL",  "TP.Select"),
    0xF6: ("F6DATA2N",  "TP.Status.SoundLevel (multi-byte: 2 args)"),
    0xF7: ("PHTRKSEL",  "BG.Select"),
    0xF8: ("F8DATA3N",  "TP.Status.MediaInfo (multi-byte: 3 args)"),
    0xF9: ("TPTRKCLR",  "TP.Reject"),
    0xFA: ("FADATA5N",  "TP.Status.Counter (multi-byte: 5 args)"),
    0xFB: ("PHTRKCLR",  "BG.Reject"),
    0xFC: ("RELEASE",   "Sys.Release"),
    0xFD: ("WAIT",      "BG.Status.Load"),
    0xFE: ("ILLEGAL",   "Sys.InvalidCmd"),
    0xFF: ("ERROR",     "INVALID"),
}


# ============================================================================
# DL'86 decode tables.
#
# Two 17-bit forms are recognized:
#   (a) BeoBabble AC form     "10.00.<5-addr>.<8-Beo4key>"
#   (b) Empirical form        "00000000.1.<8-Beo4key>"  (verified on at
#                                                       least one DL'86
#                                                       music system)
# AAC (22 bits): "10.10.<5-To>.<5-From>.<8-cmd>"
# AUC (20 bits): "10.00.<5-addr>.<3-unit>.<8-cmd>"
# Status frames are typically 40 bits.
# ============================================================================

# Beo4 key codes (subset most useful for music systems).
BEO4_KEYS: dict[int, str] = {
    0x0C: "STANDBY",
    0x1E: "Step Up (next)",
    0x1F: "Step Down (prev)",
    0x32: "Rewind",
    0x34: "Wind",
    0x35: "Go / Play",
    0x36: "Stop",
    0x60: "Volume Up",
    0x64: "Volume Down",
    0x80: "Light",
    0x81: "Radio",
    0x82: "TV",
    0x83: "AUX",
    0x86: "Tape",
    0x91: "A.MEM",
    0x92: "CD",
    0x93: "N.Radio",
    0x94: "N.Music",
    0xA1: "N.Radio (alt)",
}

# DL'86 5-bit source/destination address codes (per BeoBabble docs).
DL86_ADDRESSES: dict[int, str] = {
    0b00001: "MCL",
    0b01111: "Radio (default)",
    0b10010: "CD",
    0b11110: "Radio / BM",
}


# DL'86 STATUS payload subtypes. The 4 high bits of the 24-bit payload
# tell us which decoding rule applies. Known subtypes from BeoBabble +
# captured traffic; mark unknowns as "?subtype".
DL86_STATUS_SUBTYPES: dict[int, str] = {
    0b0100: "Status",
    0b0110: "RecStatus",
    0b1100: "Status?",         # appears in some captures, semantics TBD
    0b0000: "Idle?",           # all-zero payload, "no change"-style ping
}


# Within a STATUS frame, the meaning of d1 / d2 depends on the
# (from, to, subtype, modifier) tuple. We don't have a full spec, so we
# only decode the cases we've actually verified on real bus traffic;
# everything else falls through to the raw hex display.
#
# Verified mappings:
#   from=BM(11110), to=CD(10010), sub=Status(0100), mod=0:
#       d2 = currently-playing track number (1-indexed; 0 = no track)
#       d1 = transport state (0x00 observed as "playing"; 0x40 / 0x48
#            observed during source-switch / no-disc states but exact
#            semantics not yet pinned down)
#
# Add new entries via the helper below as more bus captures are
# verified.
def _status_payload_extra(from_addr: int, to_addr: int, sub: int,
                          mod: int, d1: int, d2: int) -> str:
    """Return a short suffix to append to the STATUS summary line, or
    "" if we don't have a contextual interpretation.

    Verified mod values (all observed for from=BM(11110), to=CD(10010),
    sub=Status(0100)):
        mod = 0x0     track-number message
                        d1 = transport state (0x00 = playing OK)
                        d2 = currently-playing track number, 1-indexed
                             (0 = no track)
        mod = 0x8     volume message
                        d2 = volume level (B&O scale, 0..~72 in 2-step
                             increments)
                        d1 = redundant: (d2 << 1) | 0x40
    """
    # BM -> {CD, MCL, ...}  Status (sub=0100).
    # The (mod, d1, d2) layout is consistent across destinations:
    #
    #   mod = 0x0   "current item" / transport state.
    #               d1 = transport state code:
    #                   0x00  playing      (d2 = item number, 1-indexed)
    #                   0x30  lid open / loading  (CD source, d2=0)
    #                   0x40  stopped      (CD source, d2=0)
    #                   0x02  Sys.Standby announcement to MCL (d2=0xFF)
    #               d2 = item number when d1=0x00, else 0.
    #   mod = 0x8   volume level. d2 is the level (0..78 typ.);
    #               d1 = (2*d2 + 0x40) & 0xFF (redundant).
    #
    # All cases verified on a real B&O music system 2026-05-02.
    if from_addr == 0b11110 and sub == 0b0100:
        if mod == 0x0:
            if to_addr == 0b00001 and d1 == 0x02 and d2 == 0xFF:
                return "  Sys.Standby"
            transport = {
                0x00: "playing",
                0x30: "lid-open / loading",
                0x40: "stopped",
            }.get(d1)
            if d1 == 0x00:
                return f"  playing item={d2}" if d2 else "  playing  no item"
            if transport is not None:
                return f"  {transport}"
            return f"  state=0x{d1:02X} item=0x{d2:02X}"
        if mod == 0x8:
            expected = (2 * d2 + 0x40) & 0xFF
            tag = "" if d1 == expected else f" (d1=0x{d1:02X} unexpected)"
            return f"  volume={d2}{tag}"
        return f"  ?mod=0x{mod:01X} d1=0x{d1:02X} d2=0x{d2:02X}"

    return ""


# ============================================================================
# small helpers
# ============================================================================

class Ansi:
    RESET   = "\033[0m"
    DIM     = "\033[2m"
    BRIGHT  = "\033[1m"
    GREEN   = "\033[32m"
    CYAN    = "\033[36m"
    MAGENTA = "\033[35m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    RED     = "\033[31m"
    GREY    = "\033[90m"


def _hex_to_bits(bit_count: int, hex_str: str) -> str:
    """Convert (bit_count, packed-hex) into the bit string the wire saw,
    MSB-first, taking the first `bit_count` bits."""
    raw = bytes.fromhex(hex_str)
    bits = "".join(f"{b:08b}" for b in raw)
    return bits[:bit_count]


# ============================================================================
# DL'86 decoding
# ============================================================================

@dataclass
class DL86Decoded:
    fmt: str               # "sys", "auxcd", "auxcd+pfx", "AC", "AAC",
                           # "STATUS", "raw"
    summary: str           # one-line summary
    fields: List[Tuple[str, str]]   # (label, value) for verbose mode


def decode_dl86(bit_count: int, hex_str: str) -> DL86Decoded:
    """Decode a DL'86 bit stream into a (format, summary, field-list)."""
    bits = _hex_to_bits(bit_count, hex_str)

    if bit_count == 6:
        # sys-level commands. Known: 100101 = sys.standby
        if bits == "100101":
            return DL86Decoded("sys", "Sys.Standby", [
                ("bits", bits), ("hex", "0x25"),
            ])
        return DL86Decoded("sys", f"sys.unknown({bits})",
                           [("bits", bits)])

    if bit_count == 8:
        # auxcd-style short. Known: 0x20 = AuxCD.Play
        b = int(bits, 2) if bits else 0
        name = "AuxCD.Play" if b == 0x20 else f"auxcd.unknown(0x{b:02X})"
        return DL86Decoded("auxcd", name, [
            ("bits", bits), ("byte", f"0x{b:02X}"),
        ])

    if bit_count == 12:
        # auxcd with explicit "0001" prefix (4 bits) + 8-bit cmd
        prefix = bits[:4]
        cmd_bits = bits[4:]
        b = int(cmd_bits, 2) if cmd_bits else 0
        name = "AuxCD.Play" if b == 0x20 else f"auxcd.unknown(0x{b:02X})"
        return DL86Decoded("auxcd+pfx",
                           f"{name} (prefix={prefix})",
                           [("prefix", prefix),
                            ("cmd",    f"{cmd_bits} = 0x{b:02X}")])

    if bit_count == 17:
        # Two forms recognized.
        #  (a) Empirical:  00000000 . 1 . <8-bit Beo4 key>
        if bits.startswith("00000000") and bits[8] == "1":
            key = int(bits[9:], 2)
            name = BEO4_KEYS.get(key, f"Beo4.unknown(0x{key:02X})")
            return DL86Decoded("AC-empir",
                f"Beo4={name} (0x{key:02X})  empirical-form",
                [("prefix",   bits[:9]),
                 ("Beo4 key", f"{bits[9:]} = 0x{key:02X}  {name}")])
        #  (b) BeoBabble AC:  10 . 00 . <5-addr> . <8-cmd>
        if bits[:2] == "10" and bits[2:4] == "00":
            addr = int(bits[4:9], 2)
            cmd  = int(bits[9:], 2)
            addr_name = DL86_ADDRESSES.get(addr, f"addr=0x{addr:02X}")
            cmd_name  = BEO4_KEYS.get(cmd, f"cmd=0x{cmd:02X}")
            return DL86Decoded("AC",
                f"AC  to={addr_name}  cmd={cmd_name}",
                [("type",     "AC (10.00.<addr>.<cmd>)"),
                 ("address",  f"{bits[4:9]} = {addr_name}"),
                 ("command",  f"{bits[9:]} = 0x{cmd:02X}  {cmd_name}")])
        return DL86Decoded("AC?", f"unknown 17-bit form ({bits})",
                           [("bits", bits)])

    if bit_count == 20:
        # AUC: 10 . 00 . <5-addr> . <3-unit> . <8-cmd>
        if bits[:4] == "1000":
            addr = int(bits[4:9], 2)
            unit = int(bits[9:12], 2)
            cmd  = int(bits[12:], 2)
            addr_name = DL86_ADDRESSES.get(addr, f"addr=0x{addr:02X}")
            cmd_name  = BEO4_KEYS.get(cmd, f"cmd=0x{cmd:02X}")
            return DL86Decoded("AUC",
                f"AUC  to={addr_name}+u{unit}  cmd={cmd_name}",
                [("type", "AUC (10.00.<addr>.<unit>.<cmd>)"),
                 ("address", f"{bits[4:9]} = {addr_name}"),
                 ("unit",    f"{bits[9:12]} = {unit}"),
                 ("command", f"{bits[12:]} = 0x{cmd:02X}  {cmd_name}")])
        return DL86Decoded("?20", f"unknown 20-bit form ({bits})",
                           [("bits", bits)])

    if bit_count == 22:
        # AAC: 10 . 10 . <5-To> . <5-From> . <8-cmd>
        if bits[:4] == "1010":
            to_addr   = int(bits[4:9], 2)
            from_addr = int(bits[9:14], 2)
            cmd       = int(bits[14:], 2)
            to_name   = DL86_ADDRESSES.get(to_addr, f"0x{to_addr:02X}")
            from_name = DL86_ADDRESSES.get(from_addr, f"0x{from_addr:02X}")
            cmd_name  = BEO4_KEYS.get(cmd, f"cmd=0x{cmd:02X}")
            return DL86Decoded("AAC",
                f"AAC  to={to_name}  from={from_name}  cmd={cmd_name}",
                [("type", "AAC (10.10.<To>.<From>.<cmd>)"),
                 ("to",      f"{bits[4:9]} = {to_name}"),
                 ("from",    f"{bits[9:14]} = {from_name}"),
                 ("command", f"{bits[14:]} = 0x{cmd:02X}  {cmd_name}")])
        return DL86Decoded("?22", f"unknown 22-bit form ({bits})",
                           [("bits", bits)])

    if bit_count == 40:
        # Status frame: 00 . 1110 . <5-from> . <5-to> . <4-sub> . <4-mod> . <8-d1> . <8-d2>
        if bits[:6] == "001110":
            from_addr = int(bits[6:11], 2)
            to_addr   = int(bits[11:16], 2)
            sub       = int(bits[16:20], 2)
            mod       = int(bits[20:24], 2)
            d1        = int(bits[24:32], 2)
            d2        = int(bits[32:40], 2)
            from_name = DL86_ADDRESSES.get(from_addr, f"0x{from_addr:02X}")
            to_name   = DL86_ADDRESSES.get(to_addr,   f"0x{to_addr:02X}")
            sub_name  = DL86_STATUS_SUBTYPES.get(sub, f"?sub{sub:01X}")
            extra     = _status_payload_extra(from_addr, to_addr, sub,
                                              mod, d1, d2)
            # Compact summary -- if we have a contextual interpretation
            # (e.g. track number) put that up front; otherwise just show
            # the data bytes.
            summary = (f"STATUS  from={from_name}  to={to_name}  "
                       f"{sub_name}")
            if extra:
                summary += extra
            else:
                summary += f"  d1=0x{d1:02X}  d2=0x{d2:02X}"
            return DL86Decoded("STATUS", summary, [
                ("type",     "STATUS (00.1110.<from>.<to>.<sub>.<mod>.<d1>.<d2>)"),
                ("from",     f"{bits[6:11]} = 0x{from_addr:02X}  {from_name}"),
                ("to",       f"{bits[11:16]} = 0x{to_addr:02X}  {to_name}"),
                ("subtype",  f"{bits[16:20]} = 0x{sub:01X}  {sub_name}"),
                ("modifier", f"{bits[20:24]} = 0x{mod:01X}"),
                ("data1",    f"{bits[24:32]} = 0x{d1:02X}"),
                ("data2",    f"{bits[32:40]} = 0x{d2:02X}"
                             + (f"  ({extra.strip()})" if extra else "")),
            ])
        return DL86Decoded("?40", f"unknown 40-bit form ({bits[:20]}...)",
                           [("bits", bits)])

    return DL86Decoded("raw", f"{bit_count}b (no decoder)",
                       [("bits", bits)])


# ============================================================================
# DL'80 decoding (single byte)
# ============================================================================

@dataclass
class DL80Decoded:
    byte: int
    mnemonic: str
    name: str

def decode_dl80(byte: int) -> DL80Decoded:
    mnem, name = DL80_OPCODES.get(byte, ("UNKNOWN", f"unknown(0x{byte:02X})"))
    return DL80Decoded(byte, mnem, name)


# ============================================================================
# Formatter
# ============================================================================

class Formatter:
    def __init__(self, color: bool, verbose: bool):
        self.color = color
        self.verbose = verbose

    def _c(self, s: str, code: str) -> str:
        if not self.color:
            return s
        return f"{code}{s}{Ansi.RESET}"

    def _ts(self) -> str:
        t = datetime.datetime.now()
        return self._c(t.strftime("%H:%M:%S.%f")[:-3], Ansi.GREY)

    def _dir(self, direction: str) -> str:
        if direction == "rx":
            return self._c("RX", Ansi.GREEN)
        if direction == "tx":
            return self._c("TX", Ansi.YELLOW)
        return direction.upper()

    def format_dl80(self, direction: str, byte: int, dup_count: int = 1) -> str:
        d = decode_dl80(byte)
        proto = self._c("dl80", Ansi.CYAN)
        line = (f"[{self._ts()}] {proto} {self._dir(direction)}  "
                f"0x{byte:02X}  "
                f"{self._c(d.name, Ansi.BRIGHT)}")
        if d.mnemonic:
            line += f"  {self._c(d.mnemonic, Ansi.GREY)}"
        if dup_count > 1:
            line += f"  {self._c(f'×{dup_count}', Ansi.GREY)}"
        return line

    def format_dl86(self, direction: str, bit_count: int, hex_str: str) -> str:
        d = decode_dl86(bit_count, hex_str)
        proto = self._c("dl86", Ansi.MAGENTA)
        line = (f"[{self._ts()}] {proto} {self._dir(direction)}  "
                f"{bit_count:>2}b {hex_str.upper():<10}  "
                f"{self._c(d.summary, Ansi.BRIGHT)}")
        if not self.verbose:
            return line
        for label, val in d.fields:
            line += f"\n    {self._c(label.ljust(10), Ansi.GREY)} {val}"
        return line


# ============================================================================
# DL'80 dedup helper
# ============================================================================

class DL80Deduper:
    """Each DL'80 byte arrives twice (the DL'80 spec says a sender repeats
    every command with an 8u HIGH gap; the broker forwards both). This
    coalesces consecutive identical bytes that arrive within `window_ms`."""
    def __init__(self, window_ms: int = 200):
        self.window = datetime.timedelta(milliseconds=window_ms)
        self._lock = threading.Lock()
        self._pending: Optional[Tuple[str, int, datetime.datetime, int]] = None
        self._flush_cb = None  # set by caller

    def submit(self, direction: str, byte: int,
               on_emit) -> None:
        """Call on_emit(direction, byte, dup_count) when a coalesced
        message is ready."""
        now = datetime.datetime.now()
        with self._lock:
            if self._pending is None:
                self._pending = (direction, byte, now, 1)
                # schedule a delayed flush
                threading.Timer(self.window.total_seconds() + 0.05,
                                self._timeout, args=(on_emit,)).start()
                return
            pdir, pbyte, pts, pcount = self._pending
            if pdir == direction and pbyte == byte and (now - pts) < self.window:
                self._pending = (pdir, pbyte, pts, pcount + 1)
                return
            # different message arrived -- flush old, start new
            self._pending = None
            pflush = (pdir, pbyte, pcount)
            self._pending = (direction, byte, now, 1)
            threading.Timer(self.window.total_seconds() + 0.05,
                            self._timeout, args=(on_emit,)).start()
        on_emit(pflush[0], pflush[1], pflush[2])

    def _timeout(self, on_emit) -> None:
        now = datetime.datetime.now()
        with self._lock:
            if self._pending is None:
                return
            pdir, pbyte, pts, pcount = self._pending
            if (now - pts) < self.window:
                return  # still aggregating
            self._pending = None
        on_emit(pdir, pbyte, pcount)


# ============================================================================
# listener
# ============================================================================

def listen(redis_host: str, redis_port: int,
           channels: Iterable[Tuple[str, str, str]],
           formatter: Formatter,
           dedup_dl80: bool,
           stop: threading.Event) -> None:
    """`channels` items: (redis_channel, protocol, direction).
    `protocol` is 'dl80' or 'dl86'. `direction` is 'rx' or 'tx'."""
    r = redis.StrictRedis(host=redis_host, port=redis_port, db=0,
                          socket_keepalive=True)
    deduper = DL80Deduper(window_ms=200) if dedup_dl80 else None

    def emit_dl80(direction: str, byte: int, dup_count: int) -> None:
        print(formatter.format_dl80(direction, byte, dup_count), flush=True)

    pubsub: Optional[redis.client.PubSub] = None
    try:
        while not stop.is_set():
            try:
                if pubsub is None:
                    pubsub = r.pubsub()
                    pubsub.subscribe(*[ch for ch, _, _ in channels])
                m = pubsub.get_message(timeout=0.5,
                                       ignore_subscribe_messages=True)
                if m is None:
                    continue
                ch = m.get("channel")
                if isinstance(ch, bytes):
                    ch = ch.decode("utf-8", errors="replace")
                proto, direction = next(
                    ((p, d) for c, p, d in channels if c == ch),
                    ("?", "?"))
                data = m.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                data = data.strip()

                if proto == "dl80":
                    try:
                        b = int(data, 16) & 0xFF
                    except ValueError:
                        print(f"[dl-debug] bad dl80 hex on {ch}: {data!r}",
                              file=sys.stderr)
                        continue
                    if deduper is not None:
                        deduper.submit(direction, b, emit_dl80)
                    else:
                        emit_dl80(direction, b, 1)
                elif proto == "dl86":
                    # broker payload format: "<bit_count>:<hex>"
                    if ":" in data:
                        a, _, b_ = data.partition(":")
                        try:
                            bit_count = int(a)
                            hex_str = b_.strip()
                        except ValueError:
                            print(f"[dl-debug] bad dl86 form on {ch}: {data!r}",
                                  file=sys.stderr)
                            continue
                    else:
                        # fallback: pure hex -> bit_count = 8 * len
                        hex_str = data
                        try:
                            bit_count = 8 * (len(bytes.fromhex(hex_str)))
                        except ValueError:
                            print(f"[dl-debug] bad dl86 hex on {ch}: {data!r}",
                                  file=sys.stderr)
                            continue
                    print(formatter.format_dl86(direction, bit_count, hex_str),
                          flush=True)
            except redis.exceptions.RedisError as e:
                print(f"[dl-debug] redis error: {e}", file=sys.stderr)
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


# ============================================================================
# CLI
# ============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)

    dgrp = ap.add_mutually_exclusive_group()
    dgrp.add_argument("--rx-only", action="store_true",
                      help="only show messages from broker -> host")
    dgrp.add_argument("--tx-only", action="store_true",
                      help="only show messages from host -> broker")

    pgrp = ap.add_mutually_exclusive_group()
    pgrp.add_argument("--dl80-only", action="store_true",
                      help="only show DL'80 traffic")
    pgrp.add_argument("--dl86-only", action="store_true",
                      help="only show DL'86 traffic")

    ap.add_argument("-v", "--verbose", action="store_true",
                    help="DL'86 verbose mode -- expand bit fields per line")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color (auto-disabled if stdout isn't a TTY)")
    ap.add_argument("--no-dedup-dl80", action="store_true",
                    help="show both copies of each DL'80 byte (default coalesces)")
    args = ap.parse_args()

    color = (not args.no_color) and sys.stdout.isatty()
    formatter = Formatter(color=color, verbose=args.verbose)

    channels: List[Tuple[str, str, str]] = []
    if not args.dl86_only:
        if not args.tx_only:
            channels.append(("link:dl80:receive",  "dl80", "rx"))
        if not args.rx_only:
            channels.append(("link:dl80:transmit", "dl80", "tx"))
    if not args.dl80_only:
        if not args.tx_only:
            channels.append(("link:dl86:receive",  "dl86", "rx"))
        if not args.rx_only:
            channels.append(("link:dl86:transmit", "dl86", "tx"))

    if not channels:
        print("[dl-debug] no channels selected (impossible flag combo)",
              file=sys.stderr)
        return 2

    stop = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    print(f"[dl-debug] subscribing to: {', '.join(c for c, _, _ in channels)}",
          file=sys.stderr)
    listen(args.redis_host, args.redis_port, channels, formatter,
           dedup_dl80=not args.no_dedup_dl80, stop=stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
