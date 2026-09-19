"""Outgoing-telegram builders.

Each builder returns a `bytes` object ready to be hex-encoded and pushed to
`link:ml:transmit`. The broker / MCU handle the wire-level checksum and
0x00 end-marker, so we never include those.

For telegrams we know work in the field (the multi-message handshakes
performed by the legacy AM and SC scripts) we preserve the exact byte
sequences and only parameterize the bytes that vary per call (FROM/TO,
source byte, ASCII text, BCD time). Where the legacy literals had obvious
bugs (e.g. wrong FROM, off-by-one pl_len) we keep the original byte for
behavioral compatibility -- a separate cleanup pass can audit those once
we have live captures to compare against.
"""
from __future__ import annotations

from datetime import datetime
import re

from core.telegram import (
    ADDR_ALL, ADDR_ALL_LINK, ADDR_AM, ADDR_SC, ADDR_SC_AUX, ADDR_VM,
    PT_CLOCK, PT_DISPLAY_SOURCE, PT_DISTRIBUTION_REQUEST,
    PT_EXT_SOURCE_INFO, PT_GOTO_SOURCE, PT_LOCK_MANAGER,
    PT_MASTER_PRESENT, PT_RELEASE, PT_REQ_DISTRIBUTED_SOURCE,
    PT_REQ_LOCAL_SOURCE, PT_STATUS_INFO, PT_TRACK_INFO,
    PT_TRACK_INFO_LONG, PT_VIRTUAL_BEO4,
    TT_COMMAND, TT_CONFIG, TT_INFO, TT_REQUEST, TT_RESPONSE,
)


# ---------------------------------------------------------------------------
# Low-level header builder. All telegrams share this layout.

def header(*, to: int, frm: int, type_: int,
           src_dest: int = 0x00, orig_src: int = 0x00, b6: int = 0x00,
           payload_type: int, payload: bytes = b"") -> bytes:
    return bytes([to, frm, 0x01, type_, src_dest, orig_src, b6,
                  payload_type, len(payload)]) + payload


# ---------------------------------------------------------------------------
# Generic / shared builders (used by both roles).

def release(*, frm: int, to: int = ADDR_ALL) -> bytes:
    """0x11 RELEASE -- 'I'm shutting down / releasing the source'.

    Sent broadcast (TO=ALL) when our provider stops streaming. Legacy AM
    payload was [0x01]; we keep that.
    """
    return header(to=to, frm=frm, type_=TT_COMMAND,
                  payload_type=PT_RELEASE, payload=bytes([0x01]))


def virtual_beo4(*, frm: int, to: int, source_byte: int, key: int,
                 orig_src: int = 0x00) -> bytes:
    """0x20 VIRTUAL_BEO4 -- generic virtual-keypress builder.

    Use the wake_*() helpers below for source-activation -- they emit the
    exact byte sequences observed on a real bus (with the FROM swapped to
    our address). This generic form is for diagnostic / future use.
    """
    return header(to=to, frm=frm, type_=TT_COMMAND,
                  src_dest=0x00, orig_src=orig_src, b6=0x00,
                  payload_type=PT_VIRTUAL_BEO4,
                  payload=bytes([0x02, 0x00, 0x01, 0x00, source_byte, key]))


# ---------------------------------------------------------------------------
# Wake telegrams.
#
# A real B&O Source Center wakes the system to its source by emitting a
# virtual_beo4 telegram up to the VIDEO MASTER. Captured original-SC wake
# (N.Radio activated directly on the SC):
#
#     c0 c1 01 0a 00 47 00 20 05 02 00 01 ff ff 93
#     ^^ ^^                ^^                      ^^
#     TO=VM              orig_src=0x47 (PC)        KEY (post-payload)
#        ^^^ FROM=0xC1 -- the SC SPOOFS the AUDIO MASTER address!
#
# Three things to note:
#   1. FROM is 0xC1 (AM), not 0xC2. The real SC impersonates the AM when
#      sending wake telegrams. The actual AM on the bus sees its own FROM
#      address but ignores own-FROM echoes (standard ML behaviour), and
#      the VM happily processes the request. We do the same.
#   2. orig_src = 0x47 = "PC" source code. The SC identifies as a PC-link
#      source in this header byte. Not strictly required by the protocol
#      that we know of, but the real SC always sends this and the system
#      is known to accept it.
#   3. Payload bytes 12 and 13 are 0xff (wildcards), not 0x00.
#   4. pl_len = 5 declares 5 payload bytes; the key byte at idx 14 is
#      post-payload "junk" that the receiver reads anyway.
#
# We mimic this 1:1.

def wake_via_vm(*, key: int) -> bytes:
    """Wake VM via virtual_beo4. Mimics the captured original-SC wake
    byte-for-byte (FROM=0xC1 spoofed, orig_src=0x47, 0xff markers).
    `key` is the Beo4 source-button code (e.g. 0x93 for N.Radio).

    Use this form when emulating a BS5-SC-button-initiated source change
    (the source center is relaying a "PC" source request). For AM-button-
    initiated emulation see wake_via_vm_am_form().
    """
    return bytes([
        ADDR_VM, ADDR_AM, 0x01, TT_COMMAND,
        0x00, 0x47, 0x00,                       # src_dest, orig_src=PC, b6
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0xff, 0xff,           # 5-byte payload with ff markers
        key,                                    # post-payload key
    ])


def wake_via_vm_am_form(*, key: int) -> bytes:
    """Wake VM via virtual_beo4 in the AM-BUTTON-initiated form.

    Captured (ml-startup.txt 2026-05-02 09:18:08.728, real AM emits this
    when user presses N.Radio on the AM's own button):

        c0 c1 01 0a 00 00 00 20 05 02 00 01 00 00 93
        TO=VM, FROM=AM, orig_src=0x00 (NONE), payload markers 00 00

    Differs from the BS5-SC form (wake_via_vm) in two bytes:
        orig_src:  0x47 (PC)   -> 0x00 (NONE)
        markers:   ff ff       -> 00 00

    Semantically: orig_src=0x00 says "AM itself is initiating, not
    relaying"; orig_src=0x47 says "SC is relaying a remote PC source".
    When we spoof FROM=AM, the AM-form is the coherent story.

    Empirically (2026-05-02 cold-standby tests), the BS5-SC form does
    not consistently get VM to commit; the AM-form is what works in the
    captured AM-button flow that DOES get VM to issue REQUEST_KEY +
    GOTO_SOURCE.
    """
    return bytes([
        ADDR_VM, ADDR_AM, 0x01, TT_COMMAND,
        0x00, 0x00, 0x00,                       # src_dest, orig_src=NONE, b6
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0x00, 0x00,           # 5-byte payload with 00 markers
        key,                                    # post-payload key
    ])


def am_dist_request_spoof(*, source_byte: int) -> bytes:
    """Spoof a DISTRIBUTION_REQUEST (PT 0x6C) FROM=AM TO=SC.

    Captured (ml-startup.txt 2026-05-02 09:18:08.730, AM-button flow):

        c2 c1 01 0b a1 00 00 6c 0a 01 00 00 00 00 02 02 01 00 09

    AM emits this exactly 2 ms after the wake virtual_beo4 (see
    wake_via_vm_am_form). The DIST_REQUEST is what tells the SC to
    start broadcasting its source-claim handshake (grant, STATUS_INFO,
    DISPLAY_SOURCE, EXTENDED_SOURCE_INFORMATION). Those broadcasts on
    the bus are also what VM observes to decide it should commit
    (REQUEST_KEY + GOTO_SOURCE).

    We spoof this when our SC wants to emulate the AM-button flow: TX
    the wake, then TX this -- our own SC RX path (or an internal
    synthesize-and-dispatch shortcut) drives the handshake from there.
    """
    return bytes([
        ADDR_SC, ADDR_AM, 0x01, TT_REQUEST,
        source_byte, 0x00, 0x00,                # src_dest=src, orig_src=NONE
        PT_DISTRIBUTION_REQUEST, 0x0a,
        0x01, 0x00, 0x00, 0x00, 0x00,
        0x02, 0x02, 0x01, 0x00, 0x09,           # final 00 09 from AM-btn capture
    ])


def wake_via_am(*, frm: int, key: int) -> bytes:
    """Wake AM directly via virtual_beo4. Use in topologies without a VM.

    Reproduces the legacy SCtoAM_NRadio template (no VM was on the bus
    when this was reverse-engineered, so we don't have a captured 'real
    SC' equivalent to mimic):
        c1 c2 01 0a 00 00 00 20 05 02 00 01 00 6f <KEY>
    Byte 13 is 0x6F (RADIO marker) regardless of which actual source.
    FROM is the caller's address (no spoofing in the AM-only path -- we
    don't have evidence the AM rejects FROM=0xC2 when there's no VM).
    """
    return bytes([
        ADDR_AM, frm, 0x01, TT_COMMAND, 0x00, 0x00, 0x00,
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0x00, 0x6f,
        key,
    ])


def wake_via_am_spoof_vm(*, key: int) -> bytes:
    """Wake AM via virtual_beo4, with FROM spoofed as VM.

    Used as a fallback when wake_via_vm doesn't elicit a DIST_REQUEST.
    Theory: if VM is in deep standby and ignoring our spoofed-AM wake,
    sending a virtual_beo4 directly to AM with FROM=VM might bypass VM's
    state machine entirely -- AM's wake-on-bus path may be more
    permissive. The byte layout mirrors wake_via_vm (same payload
    pattern with 0xff markers and orig_src=0x47=PC) for consistency.

    No real-world capture confirms this exact form works; it's a
    fallback we try when the canonical wake doesn't get a response.
    """
    return bytes([
        ADDR_AM, ADDR_VM, 0x01, TT_COMMAND,
        0x00, 0x47, 0x00,                       # src_dest, orig_src=PC, b6
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0xff, 0xff,
        key,
    ])


def wake_via_addr(*, to: int, key: int) -> bytes:
    """Wake an ARBITRARY address via virtual_beo4.

    Same byte-form as wake_via_vm() (FROM=AM-spoof, orig_src=0x47 PC,
    0xff markers) with only the TO swapped -- for setups where neither
    master is the right wake target, e.g. aiming the wake at a single
    link node so only that room comes up.

    UNVERIFIED: the VM and AM forms are backed by captures of real
    hardware; this one is not. A link node is not a master and does not
    orchestrate a source change, so it may simply ignore this.
    """
    return bytes([
        to, ADDR_AM, 0x01, TT_COMMAND,
        0x00, 0x47, 0x00,                       # src_dest, orig_src=PC, b6
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0xff, 0xff,           # 5-byte payload, ff markers
        key,                                    # post-payload key
    ])


def wake_broadcast(*, key: int) -> bytes:
    """Broadcast wake (TO=ALL) when bus topology is unknown. Uses the same
    spoofed-AM / PC-marker layout as wake_via_vm() -- whichever master is
    listening should accept it."""
    return bytes([
        ADDR_ALL, ADDR_AM, 0x01, TT_COMMAND,
        0x00, 0x47, 0x00,
        PT_VIRTUAL_BEO4, 0x05,
        0x02, 0x00, 0x01, 0xff, 0xff,
        key,
    ])


def clock(*, frm: int, to: int = ADDR_ALL, dt: datetime | None = None) -> bytes:
    """0x40 CLOCK -- broadcast time/date sync.

    The B&O ML clock format is BCD-style: each field's two ASCII decimal
    digits are interpreted as one wire byte (e.g. '23' -> 0x23). The
    legacy scripts shipped this exact encoding by stuffing
    `datetime.strftime(...)` strings into a list of two-char hex elements
    and then joining; we replicate it here as actual bytes.

    The remaining payload bytes carry weekday + locale flags. We keep the
    exact bytes the legacy scripts used (0x0a 0x00 0x03 ... 0x0a) since
    they're known-accepted by every device on the bus.
    """
    dt = dt or datetime.now()

    def bcd(n: int) -> int:
        return ((n // 10) << 4) | (n % 10)

    hh, mm, ss = bcd(dt.hour), bcd(dt.minute), bcd(dt.second)
    dd, mo, yy = bcd(dt.day),  bcd(dt.month),  bcd(dt.year % 100)
    # Legacy payload layout (11 bytes after pl_len):
    #   00  0a 00 03 11 52 59 00  HH MM SS  YY DD MO  0a
    # Indexes vs the original list: the hour/min/sec lived at raw[13..15]
    # and the date at raw[17..19]. Header is 9 bytes; payload starts at 9.
    # So payload[4]=hh, payload[5]=mm, payload[6]=ss,
    # payload[8]=dd, payload[9]=mo, payload[10]=yy.
    #
    # Wait -- re-reading the legacy script:
    #   SCtoALL_respClock[13] = current_hour      (raw idx 13)
    #   SCtoALL_respClock[17] = current_day       (raw idx 17)
    # Header is 9 bytes (indices 0..8). So payload[4]=hh, payload[8]=dd.
    # The legacy literal reads (payload portion only):
    #   0a 00 03 11 52 59 00 23 12 23 0a
    #    0  1  2  3  4  5  6  7  8  9 10
    # That puts HH at payload[7]?? Hmm.
    #
    # Let me index the legacy list from the start:
    #   idx:  0    1    2    3    4    5    6    7    8    9    10   11   12   13   14   15   16   17   18   19   20
    #   val:  80   c1   01   14   00   00   00   40   0b   0b   0a   00   03   11   52   59   00   23   12   23   0a
    # So:
    #   - header[0..8] = 80 c1 01 14 00 00 00 40 0b
    #   - payload[0..10] = 0b 0a 00 03 11 52 59 00 23 12 23 0a  -- 12 bytes? but pl_len=0b=11
    # Off by one again. The legacy literal is 21 bytes total; header(9)+payload(11)=20. So one extra byte. The trailing 0a may be junk that always lands on the wire harmlessly.
    # We'll emit the canonical 11-byte payload (no trailing junk).
    #
    # Mapping HH/MM/SS into the 11-byte payload (raw idx -> payload idx):
    #   raw[13] = payload[4]  -> HH
    #   raw[14] = payload[5]  -> MM
    #   raw[15] = payload[6]  -> SS
    #   raw[17] = payload[8]  -> DD
    #   raw[18] = payload[9]  -> MO
    #   raw[19] = payload[10] -> YY
    payload = bytes([
        0x0b, 0x0a, 0x00, 0x03,
        hh, mm, ss, 0x00,
        dd, mo, yy,
    ])
    return header(to=to, frm=frm, type_=TT_RESPONSE,
                  payload_type=PT_CLOCK, payload=payload)


# ---------------------------------------------------------------------------
# AM-role builders.
#
# These reproduce the byte sequences of the legacy ml-linkspeaker-standalone
# script. Constants come from the original literals; only FROM/TO/source-byte
# vary per call.

_AM = ADDR_AM


def am_master_present_resp(*, to: int) -> bytes:
    """Reply to a slave's MASTER_PRESENT (0x04) request -- 'yes I'm here'.

    Reproduces AMtoBL_respAM:
        06 c1 01 14 00 00 00 04 03 04 01 02 01
    Legacy literal had pl_len=3 with a trailing 0x01 byte. We use the
    protocol-consistent form: pl_len=3, payload=[0x04, 0x01, 0x02], no
    trailer. Receiving devices treat any post-payload bytes as junk.
    """
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_MASTER_PRESENT,
                  payload=bytes([0x04, 0x01, 0x02]))


def am_distributed_source_resp(*, to: int) -> bytes:
    """Reply to REQ_DISTRIBUTED_SOURCE (0x08) -- 'no source distributed'.

    Subtype 0x04 = no source. The legacy literal had pl_len=0x00 with a
    trailing 0x04 byte (length/payload mismatch); we emit the consistent
    pl_len=0x01 payload=[0x04] form, which is what the protocol actually
    expects.
    """
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_REQ_DISTRIBUTED_SOURCE,
                  payload=bytes([0x04]))


def am_local_source_resp(*, to: int) -> bytes:
    """Reply to REQ_LOCAL_SOURCE (0x30) -- 'no source playing locally'.

    Same length-mismatch fix as am_distributed_source_resp above.
    """
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_REQ_LOCAL_SOURCE,
                  payload=bytes([0x04]))


def am_lock_manager_grant(*, to: int) -> bytes:
    """Reply to LOCK_MANAGER (0x5C) request with a 'transfer key' grant.

    The legacy literal had FROM=0xC0 (VM) which is almost certainly a copy-
    paste bug -- AM responding from VM's address makes no protocol sense.
    We emit FROM=AM here. If a particular speaker rejects this we revisit.
    """
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_LOCK_MANAGER,
                  payload=bytes([0x02, 0x01]))


def am_goto_source_status_broadcast(*, source_byte: int) -> bytes:
    """0x87 STATUS_INFO broadcast announcing AM is now driving `source_byte`.

    First message of the three-step GOTO_SOURCE handshake. Sent to ALL_LINK
    so every device on the bus updates its 'currently distributed source'.
    Byte sequence is the legacy literal with the source byte parameterized.
    """
    payload = bytes([
        0x04, source_byte, 0x01, 0x00, 0x00, 0x00, 0x7e,
        0x01, 0x01, 0x00, 0x01, 0x01, 0x02, 0x01,
        0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
    ])
    return header(to=ADDR_ALL_LINK, frm=_AM, type_=TT_RESPONSE,
                  src_dest=0x00, orig_src=source_byte,
                  payload_type=PT_STATUS_INFO, payload=payload)


def am_goto_source_track_change(*, to: int, source_byte: int) -> bytes:
    """0x44 TRACK_INFO unicast follow-up telling the requesting slave which
    source is now active. Second message of the GOTO_SOURCE handshake."""
    payload = bytes([
        0x05, 0x02, source_byte, 0x00, 0x02, 0x01,
        0x00, 0x01, 0x00, 0x00, 0x00,
    ])
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_TRACK_INFO, payload=payload)


def am_track_info_long(*, to: int, source_byte: int) -> bytes:
    """0x82 TRACK_INFO_LONG -- third / final message of the handshake.

    After we send this the slave will start playing audio it receives over
    the ML bus. We don't actually distribute audio over ML on this hardware
    (the speaker takes its analog feed elsewhere), but the protocol still
    requires this telegram for the slave to consider the source active.
    """
    payload = bytes([
        0x01, 0x06, source_byte, 0x01, 0x02, 0x01,
        0x00, 0x00, 0xff, 0xff,
    ])
    return header(to=to, frm=_AM, type_=TT_RESPONSE,
                  payload_type=PT_TRACK_INFO_LONG, payload=payload)


# ---------------------------------------------------------------------------
# SC-role builders.

_SC = ADDR_SC


def sc_distribution_grant(*, to: int, source_byte: int) -> bytes:
    """Reply to a master's DISTRIBUTION_REQUEST (0x6C) granting the source.

    Reproduces the legacy SCtoAM_respNR byte sequence:
        c1 c2 01 14 00 a1 00 6c 01 08 01
    (Header places the source byte at offset 5 = orig_src, NOT offset
    4 = src_dest.)

    The legacy literal hard-coded orig_src = 0xa1 (N.RADIO) because that
    was the only source it ever served. Answering a request for a
    DIFFERENT source with 0xa1 claims we granted something the master
    never asked for -- seen live: AM requested 0x7a N.MUSIC and we
    replied `c1c2011400a1006c0108`. Echo the requested byte instead.
    """
    return header(to=to, frm=_SC, type_=TT_RESPONSE,
                  src_dest=0x00, orig_src=source_byte, b6=0x00,
                  payload_type=PT_DISTRIBUTION_REQUEST,
                  payload=bytes([0x08]))


def sc_display_source(*, source_byte: int, source_name: str,
                      sequence: int = 1) -> bytes:
    """0x06 DISPLAY_SOURCE broadcast -- tells every display the source's
    printable name.

    The real B&O Source Center sends this telegram TWICE in succession
    during the source-claim handshake: first with `sequence=1` (byte at
    payload[2] = 0x01), then with `sequence=2` (payload[2] = 0x02). The
    receiver appears to need the 1->2 pattern to fully "lock in" the
    source name on the front panel.

    Reproduces captured real-SC bytes byte-for-byte:
        83 c2 01 2c 00 SRC 00 06 11 00 03 SEQ 01 00 00 <12 ASCII chars>

    Note: pl_len is 0x11 (17) but the actual payload ships 18 bytes
    (6-byte prefix + 12-byte ASCII name). Real SC has this off-by-one;
    we replicate it because the receiver appears to expect it.
    """
    name = _pad_source_name(source_name, width=12).encode("ascii")
    # 6-byte prefix + 12-byte name = 18 bytes. pl_len declared as 17 to
    # match real-SC's off-by-one.
    return bytes([
        ADDR_ALL_LINK, _SC, 0x01, TT_INFO,
        0x00, source_byte, 0x00,
        PT_DISPLAY_SOURCE, 0x11,                # pl_len = 17 (one less)
        0x00, 0x03, sequence, 0x01, 0x00, 0x00, # 6-byte prefix
    ]) + name                                    # 12-byte name


def sc_status_info(*, source_byte: int, activity: int = 0x02) -> bytes:
    """0x87 STATUS_INFO -- the SC's "I am here, my source state is X"
    broadcast. Used for both:

      - boot-time announce: source_byte=0x47 (PC), activity=0x00 (Unknown)
      - source-active broadcast: source_byte=<our source>, activity=0x02 (Playing)

    The two forms differ only in two bytes of the payload: byte 1
    (source) and byte 12 (activity). All other bytes are constant.

    Reproduces a real B&O Source Center's 31-byte payload byte-for-byte.
    Captured boot announce:
        83c20114004700871f 04 47 0100001fbe01000000ff 00 0100030101010300020000000001000000
    Captured source-active:
        83c2011400a100871f 04 a1 0100001fbe01000000ff 02 0100030101010300020000000001000000
                                                       ^^ activity byte at idx 12

    The legacy ml-netprovide template sent only 26 bytes (pl_len 0x1a)
    AND had the activity flag in the wrong position. Both fixed here.
    """
    payload = bytes([
        0x04, source_byte, 0x01, 0x00, 0x00, 0x1f, 0xbe,
        0x01, 0x00, 0x00, 0x00, 0xff, activity, 0x01, 0x00,
        0x03, 0x01, 0x01, 0x01, 0x03, 0x00, 0x02, 0x00,
        0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00, 0x00,
    ])
    return header(to=ADDR_ALL_LINK, frm=_SC, type_=TT_RESPONSE,
                  src_dest=0x00, orig_src=source_byte,
                  payload_type=PT_STATUS_INFO, payload=payload)


# ---------------------------------------------------------------------------
# Boot-time announce sequence.
#
# Real B&O Source Centers (captured: BeoSound 5) emit a 4-message sequence
# at power-up that establishes their existence on the bus. Without this,
# VM/AM don't have us in their device list, and our later wake / source
# claim telegrams arrive as "out of band" — sometimes accepted in lenient
# bus states, but apparently sometimes interpreted as malformed and put
# the bus into a degraded state until power-cycled.
#
# Captured sequence (BeoSound 5 power-on → ready):
#
#   1. SC aux 0x02 → ALL  TT_CONFIG  PT=08 REQUEST_DIST_SOURCE  pl_len=05
#      8002015e000000080501024732defe
#      "device 0x02 here, has source 0x47 (PC)"
#
#   2. SC main 0xC2 → ALL_LK  TT_RESPONSE  PT=87 STATUS_INFO  pl_len=1f
#      83c20114004700871f04470100001fbe01000000ff00010003010101030002000000000100000000
#      = sc_status_info(source_byte=0x47, activity=0x00)
#
#   3. SC aux 0x02 → VM  TT_REQUEST  PT=04 MASTER_PRESENT  pl_len=03
#      c002010b0000000403040a0100
#
#   4. SC aux 0x02 → AM  TT_REQUEST  PT=04 MASTER_PRESENT  pl_len=03
#      c102010b0000000403040a0100


def sc_boot_status_info_pc() -> bytes:
    """Boot-time SC STATUS_INFO announce: source=PC (0x47), activity=Unknown.

    Has ONE extra trailing 0x00 byte vs the active-source STATUS_INFO --
    captured boot real-SC output ships pl_len=0x1f (31) but 32 actual
    payload bytes. We replicate that exactly. Whether the receiver cares
    about that one extra byte is unknown, but matching the capture
    byte-for-byte is the safest path.

    Captured: 83c20114004700871f04470100001fbe01000000ff00010003010101030002000000000100000000007d00
    """
    return bytes([
        ADDR_ALL_LINK, _SC, 0x01, TT_RESPONSE,
        0x00, 0x47, 0x00,                       # src_dest=00, orig_src=PC
        PT_STATUS_INFO, 0x1f,                   # pl_len = 31
        # 32 actual payload bytes (legacy off-by-one):
        0x04, 0x47, 0x01, 0x00, 0x00, 0x1f, 0xbe,
        0x01, 0x00, 0x00, 0x00, 0xff, 0x00, 0x01, 0x00,
        0x03, 0x01, 0x01, 0x01, 0x03, 0x00, 0x02, 0x00,
        0x00, 0x00, 0x00,
        0x01, 0x00, 0x00, 0x00, 0x00,
        0x00,                                    # extra trailing 00
    ])


def sc_boot_aux_config() -> bytes:
    """SC's first boot-time TT_CONFIG telegram, from auxiliary 0x02.

    Bytes match captured BeoSound 5 power-on byte-for-byte:
        80 02 01 5e 00 00 00 08 05 01 02 47 32 de fe

    The 0x32 0xde tail bytes appear to be a per-device random/serial ID;
    we use captured values. The trailing 0xfe is post-payload junk per
    pl_len=05.
    """
    return bytes([
        ADDR_ALL, ADDR_SC_AUX, 0x01, TT_CONFIG,
        0x00, 0x00, 0x00,
        PT_REQ_DISTRIBUTED_SOURCE, 0x05,
        0x01, ADDR_SC_AUX, 0x47, 0x32, 0xde,    # 5-byte payload
        0xfe,                                    # post-payload
    ])


def sc_aux_master_present(*, to: int) -> bytes:
    """MASTER_PRESENT REQUEST from SC's auxiliary 0x02 to a master.

    Used during boot-time announce to "introduce" ourselves to VM and
    AM. Byte-for-byte match to captured BeoSound 5 telegrams:
        c002010b0000000403040a0100  (to VM)
        c102010b0000000403040a0100  (to AM)

    Note the unusual payload `04 0a 01` -- the 0x0a differs from the
    `04 02 01` form VM uses to probe AM. Receivers respond to either.
    """
    return bytes([
        to, ADDR_SC_AUX, 0x01, TT_REQUEST,
        0x00, 0x00, 0x00,
        PT_MASTER_PRESENT, 0x03,
        0x04, 0x0a, 0x01,                       # 3-byte payload
        0x00,                                    # post-payload
    ])


def sc_master_present_response(*, to: int, frm: int) -> bytes:
    """MASTER_PRESENT RESPONSE -- reply to a probe from VM/AM.

    `frm` should be either ADDR_SC (0xC2) or ADDR_SC_AUX (0x02), depending
    on which of our addresses was targeted.

    Payload mirrors the form captured masters use to respond to the SC
    aux's probes. AM responds to `0x02` with `04 01 02 01`, VM responds
    with `04 02 01 01`. We use `04 0a 01 01` for the SC aux (`0x0a`
    being the marker the SC aux uses in its outgoing probes), and
    `04 02 01 01` for the SC main (matching the AM-style "self-id=01"
    pattern, swapping out the AM byte). pl_len=03 with 1 byte trailing
    is the captured shape from BeoCenter 2 / BeoSystem 3.
    """
    if frm == ADDR_SC_AUX:
        # 0a = SC aux marker (matches what 0x02 sends in requests).
        payload = bytes([0x04, 0x0a, 0x01, 0x01])
    else:
        # SC main responds with a generic "I am here" payload.
        payload = bytes([0x04, 0x02, 0x01, 0x01])
    return bytes([
        to, frm, 0x01, TT_RESPONSE,
        0x00, 0x00, 0x00,
        PT_MASTER_PRESENT, 0x03,                 # pl_len = 3
    ]) + payload                                  # 3 in-payload + 1 trailing


def sc_track_info_long(*, source_byte: int) -> bytes:
    """0x82 TRACK_INFO_LONG -- SC variant, unicast SC -> AM.

    Reproduces SCtoAM_trackinfolong03:
        c1 c2 01 14 00 00 00 82 0a 01 06 a1 00 02 00 00 00 00 00
    The legacy literal had a trailing 0x01 byte after the 10-byte payload
    which we drop -- pl_len=0x0a says only 10 payload bytes are valid.
    """
    payload = bytes([
        0x01, 0x06, source_byte, 0x00, 0x02, 0x00,
        0x00, 0x00, 0x00, 0x00,
    ])
    return header(to=ADDR_AM, frm=_SC, type_=TT_RESPONSE,
                  payload_type=PT_TRACK_INFO_LONG, payload=payload)


def sc_extended_source_info_blob(*, source_byte: int) -> bytes:
    """0x0B EXTENDED_SOURCE_INFORMATION subtype 1 -- the binary "metadata
    session start" blob the real B&O Source Center sends as the FIRST
    telegram of every metadata cycle.

    Captured layout (22 bytes of payload data after the 15-byte prefix):
        <session-id 3B> <counter 3B> 00 20 e9 90 7c 28 02 91 7c
        ff ff ff ff 22 02 91 7c

    Bytes 0..2 vary per SC boot (random session ID). Bytes 3..5 are an
    incrementing counter / timestamp that updates on each metadata
    refresh. The remaining 16 bytes are constant across captures.

    We don't know the wire-level semantics. Empirically the receiver's
    front panel won't enter metadata-display mode without first seeing
    a subtype-1 message, so we send a constant-but-realistic blob that
    matches the captured byte structure. If receivers ever start
    rejecting it we can flesh out the session-id / counter logic.
    """
    # 15-byte prefix + 23-byte blob = 38 bytes. pl_len declared as 0x25
    # (37) to match real-SC's off-by-one.
    return bytes([
        ADDR_ALL_LINK, _SC, 0x01, TT_INFO,
        0x00, source_byte, 0x00,
        PT_EXT_SOURCE_INFO, 0x25,                # pl_len = 37 (one less)
        # 15-byte prefix
        0x00, 0x01, 0x00, 0x01, 0x01, source_byte, 0x05,
        0x00, 0x00, 0x00, 0xff, 0x00, 0xff, 0x00, 0x01,
        # 23-byte blob (captured constants).
        0xd5, 0xdc, 0x07,                        # session id
        0x9c, 0xde, 0x12,                        # counter
        0x00, 0x20, 0xe9, 0x90, 0x7c, 0x28,
        0x02, 0x91, 0x7c, 0xff, 0xff, 0xff, 0xff,
        0x22, 0x02, 0x91, 0x7c,
    ])


def sc_extended_source_info(*, source_byte: int, subtype: int,
                            text: str, max_text: int = 10) -> bytes:
    """0x0B EXTENDED_SOURCE_INFORMATION carrying ASCII text.

    Reproduces the byte-for-byte layout observed from a real B&O Source
    Center on N.RADIO playback (captured SWR3 metadata):

        83 c2 01 2c 00 SRC 00 0b LL  <PREFIX(15)>  <ASCII...>

        PREFIX = 00 SUB 00 01 01 SRC 05 00 00 00 ff 00 ff 00 01
                                ^^                              <- our source byte
                    ^^                                          <- subtype 1..6
                                ^^ -- the 0x05 here may be an 'active session
                                       version' counter; we send a constant
                                       since we have no equivalent state.

    Earlier we used the legacy ml-netprovide prefix (00 SUB 00 03 01 a1
    00 00 00 03 e7 00 01 00 01) which differs from the real format on
    bytes 3, 6, 9, 10, 12 -- enough that some receivers silently ignore
    the metadata. This version matches the captured-real-SC bytes.

    Subtype meaning per source (from const.py):
        Radio   1: ""     2: Genre  3: Country  4: RDS info
                5: Beo4   6: "Unknown"
        A.Mem   1: Genre  2: Album  3: Artist   4: Track name
                5: Beo4   6: "Unknown"
    """
    text_bytes = _filter_ext_source_info_text(text, max_len=max_text).encode("ascii")
    # 15-byte prefix + N-byte text = 15+N bytes. pl_len declared as 14+N
    # to match real-SC's off-by-one (the captured "Pop" message has
    # pl_len=17 with 18 actual payload bytes).
    actual_payload_len = 15 + len(text_bytes)
    declared_pl_len = actual_payload_len - 1
    return bytes([
        ADDR_ALL_LINK, _SC, 0x01, TT_INFO,
        0x00, source_byte, 0x00,
        PT_EXT_SOURCE_INFO, declared_pl_len & 0xFF,
        # 15-byte prefix
        0x00, subtype, 0x00, 0x01, 0x01, source_byte, 0x05,
        0x00, 0x00, 0x00, 0xff, 0x00, 0xff, 0x00, 0x01,
    ]) + text_bytes


# ---------------------------------------------------------------------------
# helpers

_ALLOWED_NAME_CHARS = re.compile(r"[^A-Za-z0-9.\- ]")


def _filter_ascii(s: str, max_len: int = 15) -> str:
    """ASCII-only printable, length-clamped. Used for DISPLAY_SOURCE
    source-name padding (where punctuation like the dot in 'N.RADIO'
    is wanted). Drops anything outside A-Za-z0-9.-space."""
    return _ALLOWED_NAME_CHARS.sub("", s)[:max_len]


# Stricter character set for EXTENDED_SOURCE_INFORMATION payloads. Real
# BS5 SC captures only ever showed letters / digits / spaces in the
# subtype 2..6 text fields ("Pop", "Germany", "SWR3 Lyrix", "NONE",
# "Unknown"). Sending punctuation (or longer strings, see the 10-char
# default cap) was observed to crash the AM watchdog -- looks like a
# fixed-size buffer overflow on AM's parser. Going strict here is the
# safest landing point until/unless we find a wider charset that
# real systems demonstrably accept.
_ESI_ALLOWED_CHARS = re.compile(r"[^A-Za-z0-9 ]")


def _filter_ext_source_info_text(s: str, max_len: int = 10) -> str:
    """Strict text sanitiser for EXTENDED_SOURCE_INFORMATION payloads.

    Drops any character outside [A-Z a-z 0-9 space], collapses runs of
    whitespace, strips leading/trailing whitespace, then truncates to
    `max_len`. Default `max_len=10` matches the longest text observed in
    real-BS5-SC captures (`SWR3 Lyrix`, 10 chars).
    """
    s = _ESI_ALLOWED_CHARS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def _pad_source_name(s: str, width: int) -> str:
    """Right-pad an ASCII source name to fixed width with spaces, truncated
    to width if too long. Used in DISPLAY_SOURCE telegrams."""
    s = _filter_ascii(s, max_len=width)
    return s + (" " * (width - len(s)))
