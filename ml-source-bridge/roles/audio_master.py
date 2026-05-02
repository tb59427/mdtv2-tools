"""AUDIO MASTER (0xC1) role.

Pretend to be an Audio Master on the ML bus -- the device that sources
audio for ML link-speakers (BL3500 / BL2000 / etc., addresses 0x06 etc.)
that don't have built-in sources of their own. The link-speakers query
us with MASTER_PRESENT / REQ_LOCAL_SOURCE / etc. and we hand them the
GOTO_SOURCE handshake to make them play whatever our backend (provider)
is producing.

Telegrams handled (TO ∈ {0xC1, 0x80}):

  REQ  0x04 MASTER_PRESENT      -> respond 'I am here'
  REQ  0x08 REQ_DIST_SRC        -> respond 'no distributed source'
  REQ  0x30 REQ_LOCAL_SOURCE    -> respond 'no local source' (we don't
                                   actually advertise a local source --
                                   slaves request via GOTO_SOURCE instead)
  REQ  0x5C LOCK_MANAGER        -> grant
  REQ  0x45 GOTO_SOURCE w/ our  -> respond with the 3-message handshake
       provider's source byte      (broadcast STATUS_INFO, unicast
                                    TRACK_INFO, unicast TRACK_INFO_LONG),
                                    then tell provider to play
  CMD  0x0D BEO4_KEY (1E/1F/    -> forward as next/prev to provider
                      32/34)
  CMD  0x11 RELEASE             -> tell provider to pause
  RSP  0x3C TIMER               -> ignored (legacy radio-wake removed)
"""
from __future__ import annotations

import time

from core import builders as B
from core.bus import log
from core.dispatcher import Context, Dispatcher
from core.telegram import (
    ADDR_ALL, ADDR_AM, BEO4_KEY_FOR_SOURCE,
    KEY_REWIND, KEY_STEP_DOWN, KEY_STEP_UP, KEY_WIND,
    PT_BEO4_KEY, PT_GOTO_SOURCE, PT_LOCK_MANAGER, PT_MASTER_PRESENT,
    PT_RELEASE, PT_REQ_DISTRIBUTED_SOURCE, PT_REQ_LOCAL_SOURCE, Telegram,
)
from roles.base import Role


class AudioMasterRole(Role):
    own_address = ADDR_AM
    address_aliases = {ADDR_AM, ADDR_ALL}
    name = "am"

    def install_handlers(self, d: Dispatcher) -> None:
        d.register((PT_MASTER_PRESENT,),         self._on_master_present)
        d.register((PT_REQ_DISTRIBUTED_SOURCE,), self._on_req_dist_src)
        d.register((PT_REQ_LOCAL_SOURCE,),       self._on_req_local_src)
        d.register((PT_LOCK_MANAGER,),           self._on_lock_manager)
        d.register((PT_GOTO_SOURCE,),            self._on_goto_source)
        d.register((PT_RELEASE,),                self._on_release)
        # Per-key handlers for BEO4 next/prev/wind/rewind.
        d.register((PT_BEO4_KEY, "key", KEY_STEP_UP),   self._on_next)
        d.register((PT_BEO4_KEY, "key", KEY_STEP_DOWN), self._on_prev)
        d.register((PT_BEO4_KEY, "key", KEY_WIND),      self._on_next)
        d.register((PT_BEO4_KEY, "key", KEY_REWIND),    self._on_prev)

    # ---- handlers ----------------------------------------------------------

    def _on_master_present(self, t: Telegram, ctx: Context) -> None:
        log(f"[am] MASTER_PRESENT? from 0x{t.from_addr:02x} -- I'm here")
        ctx.bus.send(B.am_master_present_resp(to=t.from_addr))

    def _on_req_dist_src(self, t: Telegram, ctx: Context) -> None:
        log(f"[am] REQ_DIST_SRC from 0x{t.from_addr:02x} -- none")
        ctx.bus.send(B.am_distributed_source_resp(to=t.from_addr))

    def _on_req_local_src(self, t: Telegram, ctx: Context) -> None:
        log(f"[am] REQ_LOCAL_SRC from 0x{t.from_addr:02x} -- none")
        ctx.bus.send(B.am_local_source_resp(to=t.from_addr))

    def _on_lock_manager(self, t: Telegram, ctx: Context) -> None:
        log(f"[am] LOCK_MGR from 0x{t.from_addr:02x} -- granting")
        ctx.bus.send(B.am_lock_manager_grant(to=t.from_addr))

    def _on_goto_source(self, t: Telegram, ctx: Context) -> None:
        # GOTO_SOURCE carries the requested source byte at offset 11.
        requested = t.at(11)
        provider = ctx.provider_for(requested)
        if provider is None:
            log(f"[am] GOTO_SOURCE 0x{requested:02x} -- not ours "
                f"(claimed: {[hex(s) for s in ctx.claimed_sources()]}); "
                f"ignoring")
            return
        log(f"[am] GOTO_SOURCE 0x{requested:02x} from 0x{t.from_addr:02x}"
            f" -- handshake + PLAY (provider={provider.display_name!r})")
        # Single-active-source: pause any other provider still streaming
        # before this one takes over. One DAC output, one bus source.
        ctx.pause_other_providers(provider)
        # Three-message handshake. Sleeps mirror the legacy script's
        # cadence; the speakers seem to need the gaps.
        ctx.bus.send(B.am_goto_source_status_broadcast(source_byte=requested))
        time.sleep(0.25)
        ctx.bus.send(B.am_goto_source_track_change(
            to=t.from_addr, source_byte=requested))
        time.sleep(0.25)
        ctx.bus.send(B.am_track_info_long(
            to=t.from_addr, source_byte=requested))
        provider.play()

    def _on_release(self, t: Telegram, ctx: Context) -> None:
        provider = ctx.provider_for(t.src_dest)
        if provider is not None:
            log(f"[am] RELEASE 0x{t.src_dest:02x} from 0x{t.from_addr:02x}"
                f" -- pausing {provider.display_name!r}")
            provider.pause()
        elif t.src_dest == 0:
            log(f"[am] generic RELEASE from 0x{t.from_addr:02x}"
                f" -- pausing all providers")
            for p in ctx.providers.values():
                p.pause()

    def _on_next(self, t: Telegram, ctx: Context) -> None:
        provider = ctx.provider_for(t.src_dest)
        if provider is None:
            return
        log(f"[am] BEO4 next on 0x{t.src_dest:02x} -> {provider.display_name!r}")
        provider.next()

    def _on_prev(self, t: Telegram, ctx: Context) -> None:
        provider = ctx.provider_for(t.src_dest)
        if provider is None:
            return
        log(f"[am] BEO4 prev on 0x{t.src_dest:02x} -> {provider.display_name!r}")
        provider.prev()

    # ---- provider events ---------------------------------------------------

    def on_provider_event(self, ctx: Context, event: str,
                          provider: object) -> None:
        """- 'stream_started': broadcast a virtual Beo4 keypress for the
          source whose provider just started, so link-speakers switch to
          it (they then issue GOTO_SOURCE which our handler answers).
        - 'stream_stopped': broadcast RELEASE.
        AM role doesn't push metadata -- link-speakers have no display.
        """
        if event == "stream_started":
            src = provider.source_byte
            beo4 = BEO4_KEY_FOR_SOURCE.get(src)
            if beo4 is None:
                log(f"[am] no Beo4 key mapped for source 0x{src:02x}; "
                    f"can't auto-wake speakers")
                return
            # Single-active-source: pause any other provider still streaming.
            ctx.pause_other_providers(provider)
            log(f"[am] stream started on 0x{src:02x} ({provider.display_name!r})"
                f" -- waking speakers with Beo4 0x{beo4:02x}")
            ctx.bus.send(B.virtual_beo4(
                frm=ADDR_AM, to=ADDR_ALL,
                source_byte=src, key=beo4,
            ))
        elif event == "stream_stopped":
            log(f"[am] stream stopped on 0x{provider.source_byte:02x} "
                f"({provider.display_name!r}) -- broadcasting RELEASE")
            ctx.bus.send(B.release(frm=ADDR_AM))
