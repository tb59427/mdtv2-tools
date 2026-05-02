"""ML bus lock-manager protocol.

Implementation of the token-passing scheme described in Christensen &
Jorgensen's "Analysing Bang & Olufsen's BeoLink Audio/Video System Using
Coloured Petri Nets" (Aarhus DAIMI PB report, available at
tidsskrift.dk/daimipb/article/download/7043/6004/0).

PURPOSE
    The B&O ML bus uses a token (the "key") to ensure only one device at
    a time can issue commands that change source / playback / distribution
    state. Without holding the key, a sender's BEO4_KEY / source-change
    telegrams race against the legitimate key-holder and produce flaky
    results -- exactly the "track 11 instead of track 1" race the paper
    cites as motivation.

PROTOCOL (PT_LOCK_MANAGER = 0x5C, payload byte 9 = subtype)
    0x01 REQUEST_KEY        broadcast: "I want the key"
    0x02 KEY_TRANSFER       lock manager -> requester: "here it is"
    0x03 KEY_TRANSFER_IMPOS lock manager -> requester: "can't give now"
    0x04 NEW_LOCK_MANAGER   new manager -> previous manager:
                            "got it, I'm the lock manager now"  (a.k.a.
                            KEY_RECEIVED in const.py)
    0x05 KEY_LOST           broadcast: "no one answered me, key is gone"
                            (a.k.a. TIMEOUT in const.py)

POWER MASTER vs LOCK MANAGER
    These are different roles:
      - power master  = the device feeding 5 V to the bus (usually exactly
                        one device). Has the special duty of generating a
                        new key after KEY_LOST or at cold init.
      - lock manager  = whichever device currently holds the key. Migrates
                        as devices REQUEST_KEY.

OUR USAGE
    We're a slave (SC role). Before the SC role sends a wake / source-
    altering telegram it calls `acquire(timeout)`. If we already hold the
    key the call returns immediately. Otherwise we broadcast REQUEST_KEY,
    wait for KEY_TRANSFER, ack with NEW_LOCK_MANAGER, and become the
    manager. Subsequent REQUEST_KEY broadcasts from other devices are
    answered with KEY_TRANSFER (we let go).

    Captured timeout from the paper / live traffic: ~1.5 s of unanswered
    REQUEST_KEY before KEY_LOST is broadcast. We use a similar window.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

from core.bus import log
from core.telegram import (
    ADDR_ALL, PT_LOCK_MANAGER, TT_COMMAND, TT_REQUEST, TT_RESPONSE, Telegram,
)


# Lock-manager subtype bytes (payload[0]).
SUB_REQUEST_KEY        = 0x01
SUB_KEY_TRANSFER       = 0x02
SUB_TRANSFER_IMPOSSIBLE = 0x03
SUB_KEY_RECEIVED       = 0x04
SUB_KEY_LOST           = 0x05


# How long to wait for KEY_TRANSFER after broadcasting REQUEST_KEY before
# giving up. Real B&O devices use ~1.5 s (paper's KEY_LOST timeout). We
# match that.
_REQUEST_TIMEOUT_S = 1.5


# ---- builders ---------------------------------------------------------------

def _lockmgr(*, to: int, frm: int, type_: int, subtype: int) -> bytes:
    """Build a LOCK_MANAGER telegram. Captured payloads are 1-byte (the
    subtype) plus a 1-byte trailer (0x01 in observed traffic). pl_len is
    declared as 1 even though the payload-on-wire is 2 bytes -- legacy
    off-by-one we preserve to match real bus behaviour."""
    return bytes([
        to, frm, 0x01, type_,
        0x00, 0x00, 0x00,
        PT_LOCK_MANAGER, 0x01,    # pl_len = 1
        subtype, 0x01,             # payload: subtype + 1-byte trailer
    ])


def request_key(frm: int) -> bytes:
    """Broadcast: 'I want the key.'"""
    return _lockmgr(to=ADDR_ALL, frm=frm,
                    type_=TT_REQUEST, subtype=SUB_REQUEST_KEY)


def key_transfer(*, to: int, frm: int) -> bytes:
    """Lock manager -> requester: 'here is the key.'"""
    return _lockmgr(to=to, frm=frm,
                    type_=TT_RESPONSE, subtype=SUB_KEY_TRANSFER)


def transfer_impossible(*, to: int, frm: int) -> bytes:
    """Lock manager -> requester: 'cannot give the key right now.'"""
    return _lockmgr(to=to, frm=frm,
                    type_=TT_RESPONSE, subtype=SUB_TRANSFER_IMPOSSIBLE)


def key_received(*, to: int, frm: int) -> bytes:
    """New manager -> previous manager: 'got it, I'm the manager now.'"""
    return _lockmgr(to=to, frm=frm,
                    type_=TT_COMMAND, subtype=SUB_KEY_RECEIVED)


def key_lost(frm: int) -> bytes:
    """Broadcast: 'no one answered, the key is lost.'"""
    return _lockmgr(to=ADDR_ALL, frm=frm,
                    type_=TT_COMMAND, subtype=SUB_KEY_LOST)


# ---- state machine ----------------------------------------------------------

class LockManager:
    """Per-device lock-manager state. Owned by the role; receives
    notification of every incoming LOCK_MANAGER telegram via
    `on_telegram()`, and exposes `acquire()` / `release_to()` for the
    role to call before sending state-changing telegrams.

    States (mirroring the paper's notation):
        IDLE    -- we don't hold the key, don't currently want it
        WAIT    -- we broadcast REQUEST_KEY, awaiting KEY_TRANSFER
        HELD    -- we hold the key (we are the lock manager)

    Outside view of the bus:
        current_manager -- best-effort tracking of who currently holds
                           the key. Updated on every NEW_LOCK_MANAGER /
                           KEY_LOST we observe. May be None during
                           init / after KEY_LOST.
    """

    IDLE, WAIT, HELD = "idle", "wait", "held"

    def __init__(self, *, our_address: int, send_fn) -> None:
        self.our_address = our_address
        self._send = send_fn
        self._state = self.IDLE
        self._key_event = threading.Event()
        self._lock = threading.Lock()
        # Last device we observed becoming lock manager. Used purely
        # for diagnostics / logging.
        self.current_manager: Optional[int] = None

    # ---- public API -----------------------------------------------------

    def acquire(self, timeout_s: float = _REQUEST_TIMEOUT_S) -> bool:
        """Block until we hold the key, or give up after `timeout_s`.

        Returns True on success. If we already hold the key (HELD state),
        returns immediately. Otherwise broadcasts REQUEST_KEY and waits.
        """
        with self._lock:
            if self._state == self.HELD:
                log("[lock] already holding key")
                return True
            self._state = self.WAIT
            self._key_event.clear()

        log(f"[lock] requesting key (current manager: "
            f"{'?' if self.current_manager is None else hex(self.current_manager)})")
        self._send(request_key(frm=self.our_address))

        if self._key_event.wait(timeout=timeout_s):
            log(f"[lock] key acquired (we are now the lock manager)")
            return True

        # Timeout. Fall back: broadcast KEY_LOST so the power master can
        # regenerate the key. We still don't hold it -- caller will have
        # to retry or proceed without.
        with self._lock:
            self._state = self.IDLE
        log(f"[lock] timeout waiting for KEY_TRANSFER (no manager? "
            f"broadcasting KEY_LOST)", err=True)
        self._send(key_lost(frm=self.our_address))
        return False

    def is_held(self) -> bool:
        return self._state == self.HELD

    # ---- bus-event handlers ---------------------------------------------

    def on_telegram(self, t: Telegram) -> None:
        """Called by the role's dispatcher for every received
        PT_LOCK_MANAGER telegram. Drives the state machine."""
        if t.payload_type != PT_LOCK_MANAGER:
            return
        sub = t.at(9)

        if sub == SUB_REQUEST_KEY:
            self._on_request_key(t)
        elif sub == SUB_KEY_TRANSFER:
            self._on_key_transfer(t)
        elif sub == SUB_TRANSFER_IMPOSSIBLE:
            self._on_transfer_impossible(t)
        elif sub == SUB_KEY_RECEIVED:
            self._on_key_received(t)
        elif sub == SUB_KEY_LOST:
            self._on_key_lost(t)

    def _on_request_key(self, t: Telegram) -> None:
        """Another device wants the key. If we hold it, transfer it."""
        if t.from_addr == self.our_address:
            return                          # our own broadcast -- ignore
        with self._lock:
            holds = (self._state == self.HELD)
        if not holds:
            return                          # not our problem
        log(f"[lock] REQUEST_KEY from 0x{t.from_addr:02x} -- "
            f"transferring (we held)")
        self._send(key_transfer(to=t.from_addr, frm=self.our_address))
        # We expect a KEY_RECEIVED back; until then we are still the
        # manager (KEY_TRANS state in the paper). For simplicity we
        # transition immediately to IDLE -- if KEY_RECEIVED never comes,
        # current_manager will be updated when someone else's
        # KEY_RECEIVED is observed, or KEY_LOST is broadcast.
        with self._lock:
            self._state = self.IDLE
            self.current_manager = t.from_addr   # optimistic

    def _on_key_transfer(self, t: Telegram) -> None:
        """We just got handed the key (in response to our REQUEST_KEY)."""
        if t.to_addr != self.our_address:
            # This transfer was for someone else. Note who got it.
            self.current_manager = t.to_addr
            return
        with self._lock:
            if self._state != self.WAIT:
                log(f"[lock] unexpected KEY_TRANSFER (state={self._state})",
                    err=True)
                return
            self._state = self.HELD
            self.current_manager = self.our_address
        # Acknowledge to the previous manager.
        self._send(key_received(to=t.from_addr, frm=self.our_address))
        self._key_event.set()

    def _on_transfer_impossible(self, t: Telegram) -> None:
        if t.to_addr != self.our_address:
            return
        log(f"[lock] TRANSFER_IMPOSSIBLE from 0x{t.from_addr:02x}", err=True)
        # State stays WAIT; caller's `acquire()` will time out.

    def _on_key_received(self, t: Telegram) -> None:
        """Some device just acked becoming the new lock manager."""
        # The FROM is the new lock manager (the "I have it now" sender).
        if t.from_addr != self.our_address:
            self.current_manager = t.from_addr
            log(f"[lock] new lock manager: 0x{t.from_addr:02x}")

    def _on_key_lost(self, t: Telegram) -> None:
        log(f"[lock] KEY_LOST from 0x{t.from_addr:02x} -- "
            f"key gone, waiting for power master to regenerate")
        self.current_manager = None
        with self._lock:
            if self._state == self.HELD:
                # We thought we had the key but someone else thinks it
                # is lost. Drop our claim.
                self._state = self.IDLE
            elif self._state == self.WAIT:
                # We were waiting for KEY_TRANSFER and a different device
                # just declared the key lost. Our request will now time
                # out naturally; nothing more to do.
                pass
