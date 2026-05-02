"""Telegram model and well-known address / type / payload-type constants.

The wire layout we receive from the broker (link:ml:receive) and send to the
broker (link:ml:transmit) is identical except RX includes a checksum and 0x00
end-marker which we ignore on parse and don't emit on TX -- the broker /
ATtiny manage those.

Layout (offsets):
  [0]    TO            destination address
  [1]    FROM          source address
  [2]    b2            always 0x01 in observed traffic
  [3]    type          0x0A=cmd 0x0B=req 0x14=rsp 0x40=stat
  [4]    src_dest      meaning depends on payload_type (often the source byte)
  [5]    orig_src      origin source byte (often 0x00 or the active source)
  [6]    b6            usually 0x00
  [7]    payload_type  one of PT_*
  [8]    pl_len        N
  [9..]  N payload bytes
"""
from __future__ import annotations

from dataclasses import dataclass


# ---- well-known addresses ---------------------------------------------------
# Subset relevant to our two roles. Full table lives in ml-debug/const.py.
ADDR_VM        = 0xC0   # VIDEO MASTER
ADDR_AM        = 0xC1   # AUDIO MASTER          -- our AM role pretends to be this
ADDR_SC        = 0xC2   # SOURCE CENTER         -- our SC role pretends to be this
ADDR_ALL       = 0x80   # ALL DEVICES
ADDR_AAL       = 0x81   # ALL AUDIO LINK
ADDR_AVL       = 0x82   # ALL VIDEO LINK
ADDR_ALL_LINK  = 0x83   # ALL LINK DEVICES

# 0x02 is a "discovery / setup" address used by some B&O devices
# (Beosound 5, MOOD app, etc.) at power-up. Captured behaviour:
#   - device sends a TT_CONFIG telegram FROM=0x02 TO=ALL announcing its
#     existence (PT=0x08 REQUEST_DISTRIBUTED_SOURCE, with an SC's source
#     byte in the payload)
#   - device sends MASTER_PRESENT REQUESTs FROM=0x02 to VM (0xC0) and AM
#     (0xC1) to discover them
#   - both VM and AM respond TO=0x02 even when the bus is otherwise in
#     standby -- they distinguish themselves via byte 12 of the payload:
#         0x01 = Audio Master, 0x02 = Video Master
# This makes 0x02 useful as a wake-friendly probe FROM-address: masters
# answer it even in low-power states. We don't currently use it (config-
# driven topology fits our model), but documenting it here for future
# reference.
ADDR_DISCOVERY = 0x02   # "device boot announce / master discovery"
ADDR_SC_AUX    = 0x02   # SC's auxiliary address (alias of ADDR_DISCOVERY).
                        # Real B&O Source Centers use this address for
                        # boot-time CONFIG announce and master probes.
                        # Captured: BeoSound 5 (SC) used 0x02 at power-on.
ADDR_VM_AUX    = 0x6E   # VM's auxiliary address (BeoSystem 3 captured).
ADDR_AM_AUX    = 0x27   # AM's auxiliary address (BeoCenter 2 captured).


# ---- telegram type bytes (offset 3) ----------------------------------------
TT_COMMAND   = 0x0A
TT_REQUEST   = 0x0B
TT_RESPONSE  = 0x14
TT_INFO      = 0x2C
TT_CONFIG    = 0x5E


# ---- payload-type bytes (offset 7) -----------------------------------------
PT_MASTER_PRESENT          = 0x04
PT_DISPLAY_SOURCE          = 0x06
PT_REQ_DISTRIBUTED_SOURCE  = 0x08
PT_EXT_SOURCE_INFO         = 0x0B
PT_BEO4_KEY                = 0x0D
PT_STANDBY                 = 0x10
PT_RELEASE                 = 0x11
PT_VIRTUAL_BEO4            = 0x20
PT_REQ_LOCAL_SOURCE        = 0x30
PT_TIMER                   = 0x3C
PT_CLOCK                   = 0x40
PT_TRACK_INFO              = 0x44
PT_GOTO_SOURCE             = 0x45
PT_LOCK_MANAGER            = 0x5C
PT_DISTRIBUTION_REQUEST    = 0x6C
PT_TRACK_INFO_LONG         = 0x82
PT_STATUS_INFO             = 0x87
PT_PICT_SOUND_STATUS       = 0x98


# ---- selected source bytes (offset 4 / 11 depending on payload type) -------
SRC_NONE     = 0x00
SRC_PC       = 0x47   # PC link source -- real Source Centers identify
                      # as this in their boot-time STATUS_INFO announce.
SRC_RADIO    = 0x6F
SRC_A_MEM    = 0x79
SRC_N_MUSIC  = 0x7A
SRC_CD       = 0x8D
SRC_A_AUX    = 0x97
SRC_N_RADIO  = 0xA1


# ---- Beo4 key codes we actually act on -------------------------------------
KEY_STEP_UP    = 0x1E   # NEXT
KEY_STEP_DOWN  = 0x1F   # PREV
KEY_REWIND     = 0x32
KEY_WIND       = 0x34
KEY_GO_PLAY    = 0x35
KEY_STOP       = 0x36

# Source-selection Beo4 key codes (from beo4_commanddict in ml-debug/const.py).
# Used for the auto-wake feature: when the provider's audio stream starts we
# inject the matching virtual Beo4 keypress so the AM/speakers switch to our
# source without the user having to press anything on the remote.
BEO4_KEY_FOR_SOURCE = {
    SRC_RADIO:   0x81,   # "Radio"
    SRC_A_MEM:   0x91,   # "A.Mem"
    SRC_N_MUSIC: 0x94,   # "N.Music"
    SRC_CD:      0x92,   # "CD"
    SRC_N_RADIO: 0x93,   # "N.Radio"
}


# ============================================================================

@dataclass
class Telegram:
    """Parsed view of one ML telegram. Construct via from_hex(); all accessors
    return either ints (single bytes) or bytes (slices)."""
    raw: bytes

    @classmethod
    def from_hex(cls, hex_str: str) -> "Telegram":
        return cls(raw=bytes.fromhex(hex_str.strip()))

    # header fields ----------------------------------------------------------
    @property
    def to_addr(self) -> int:        return self.raw[0] if len(self.raw) > 0 else 0
    @property
    def from_addr(self) -> int:      return self.raw[1] if len(self.raw) > 1 else 0
    @property
    def telegram_type(self) -> int:  return self.raw[3] if len(self.raw) > 3 else 0
    @property
    def src_dest(self) -> int:       return self.raw[4] if len(self.raw) > 4 else 0
    @property
    def orig_src(self) -> int:       return self.raw[5] if len(self.raw) > 5 else 0
    @property
    def payload_type(self) -> int:   return self.raw[7] if len(self.raw) > 7 else 0
    @property
    def payload_len(self) -> int:    return self.raw[8] if len(self.raw) > 8 else 0

    @property
    def payload(self) -> bytes:
        """The N payload bytes after the header. Truncated to actual length."""
        n = self.payload_len
        return self.raw[9:9 + n]

    def at(self, offset: int, default: int = 0) -> int:
        """Bounds-checked single-byte read."""
        return self.raw[offset] if offset < len(self.raw) else default

    def hex(self) -> str:
        return self.raw.hex()
