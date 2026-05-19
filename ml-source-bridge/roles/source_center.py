"""SOURCE CENTER (0xC2) role.

Pretend to be a Source Center on the ML bus -- the device that provides
external sources (N.MUSIC, N.RADIO) to a music system that already has
its own internal sources (CD, FM radio, A.MEM). The Audio Master queries
us with a DISTRIBUTION_REQUEST when the user picks our source on the
remote, and we respond with a multi-message handshake to claim the
source, push display name, status, track info, and metadata text.

Telegrams handled (TO ∈ {0xC2, 0x83, 0x80}):

  REQ  0x6C DISTRIBUTION_REQUEST     -> 7-message claim handshake +
       w/ src_dest matching our         provider.play()
       provider's source byte
  CMD  0x0D BEO4_KEY (1E/1F)         -> next/prev on provider
  CMD  0x10 STANDBY                  -> provider.pause()
       (AM/VM emits this when the user switches away from our source,
        with src_dest = our source byte; without handling it AirPlay
        would keep streaming until full system shutdown.)
  CMD  0x11 RELEASE                  -> provider.pause()

We no longer try to "wake the AM" by injecting a virtual Beo4 key when
audio starts -- that responsibility belongs to whatever upstream source
selection the user makes.
"""
from __future__ import annotations

import time

from core import builders as B
from core.bus import log
from core.dispatcher import Context, Dispatcher
from core.lock_manager import LockManager
from core.telegram import (
    ADDR_ALL, ADDR_ALL_LINK, ADDR_AM, ADDR_SC, ADDR_SC_AUX, ADDR_VM,
    BEO4_KEY_FOR_SOURCE,
    KEY_STEP_DOWN, KEY_STEP_UP,
    PT_BEO4_KEY, PT_DISTRIBUTION_REQUEST, PT_LOCK_MANAGER,
    PT_MASTER_PRESENT, PT_RELEASE, PT_STANDBY,
    SRC_PC, TT_REQUEST, Telegram,
)
from roles.base import Role


class SourceCenterRole(Role):
    own_address = ADDR_SC
    # We answer to:
    #   ADDR_SC (0xC2)        -- main SC address, source-related ops
    #   ADDR_SC_AUX (0x02)    -- discovery/announce auxiliary; real BeoSound 5
    #                            uses this for boot-time master probes and
    #                            responds to incoming MASTER_PRESENT requests
    #                            from VM/AM. Without it, masters that probe
    #                            us with TO=0x02 get no response and may
    #                            time us out as "missing".
    #   ADDR_ALL_LINK (0x83)  -- broadcast to all link devices
    #   ADDR_ALL (0x80)       -- broadcast to all devices
    address_aliases = {ADDR_SC, ADDR_SC_AUX, ADDR_ALL_LINK, ADDR_ALL}
    name = "sc"

    # Wake-with-retry parameters.
    #
    # Strategy (post 2026-05-02 firmware v1.5.5 RX fix): mirror the
    # captured BS5-SC source-activation flow exactly. SC emits ONE
    # wake virtual_beo4 (TO=VM FROM=AM-spoof, PC-form), then sits
    # and waits. The real VM and AM then autonomously perform:
    #   - VM REQUEST_KEY -> AM TRANSFER_KEY -> VM KEY_RECEIVED
    #     (lock manager dance, ~5 ms)
    #   - VM and AM MASTER_PRESENT exchange (~10 ms)
    #   - VM GOTO_SOURCE N.Radio -> AM           (VM commits, ~20 ms)
    #   - AM and VM REQUEST_DISTRIBUTED_SOURCE   (~700 ms)
    #   - AM DIST_REQUEST -> SC                  (~800 ms after wake)
    # Our SC's _on_dist_request handler then runs the standard claim
    # handshake. Because VM committed, audio routes through VM's amp
    # (its speakers come on).
    #
    # No wake-to-AM stage. No synthesized DIST_REQUEST. The earlier
    # "AM-first" attempt did get AM to autonomously DIST_REQUEST the
    # SC (audio played), but skipped the VM commit, so VM stayed in
    # standby. The captured BS5-SC flow shows the wake must be
    # addressed to VM for VM to take the master role.
    _WAKE_RETRY_TIMEOUT_S = 4.0   # generous: real AM took ~800 ms in
                                  # the BS5-SC capture; we leave margin
                                  # for slower / colder bus states.
    _WAKE_MAX_ATTEMPTS  = 3       # ~12 s total budget

    # How long to log every received telegram after a wake fires, so we
    # can see exactly what (if anything) the bus does in response. Set to
    # 0 to disable the diagnostic.
    _WAKE_WATCH_SECONDS = 10.0

    # Suppress duplicate claim-handshakes if a real DIST_REQUEST from AM
    # arrives within this window after we synthesized one ourselves (or
    # vice versa). Without this we'd run the full ~600 ms handshake
    # twice and double-broadcast STATUS_INFO etc.
    _DIST_REQUEST_DEDUP_S = 2.0

    def __init__(self) -> None:
        import threading
        # Per-source-byte ack event: set when we receive DIST_REQUEST for
        # that source (proves the wake reached the system). The wake-retry
        # thread waits on this with timeout.
        self._wake_acks: dict = {}
        self._wake_acks_lock = threading.Lock()
        # Lock-manager state machine. We OBSERVE the VM<->AM lock-manager
        # dance for diagnostics (passive_observe()), but we do NOT
        # initiate or acquire the key. Confirmed from the BS5 capture:
        # SCs are never present in any LOCK_MANAGER telegram. Lock
        # manager is exclusively a VM<->AM affair.
        self._lock_mgr: object = None
        # monotonic deadline; any RX telegram arriving before this gets
        # logged with a [wake-watch] prefix in passive_observe(). Set by
        # _wake_with_retries() on each wake fire.
        self._wake_watch_until: float = 0.0
        # source_byte -> monotonic timestamp of last claim-handshake.
        # Used to dedup synthetic + real DIST_REQUEST landing close
        # together.
        self._last_handshake_at: dict[int, float] = {}

    def install_handlers(self, d: Dispatcher) -> None:
        d.register((PT_DISTRIBUTION_REQUEST,),         self._on_dist_request)
        d.register((PT_RELEASE,),                      self._on_release)
        d.register((PT_STANDBY,),                      self._on_standby)
        d.register((PT_MASTER_PRESENT,),               self._on_master_present)
        d.register((PT_BEO4_KEY, "key", KEY_STEP_UP),  self._on_next)
        d.register((PT_BEO4_KEY, "key", KEY_STEP_DOWN), self._on_prev)
        # PT_LOCK_MANAGER's real work is in passive_observe() (so VM<->AM
        # lock telegrams not addressed to us still drive our state).
        # Register a stub handler so dispatcher doesn't flag those that
        # ARE addressed to us as "unhandled REQUEST" -- passive_observe
        # already processed them.
        d.register((PT_LOCK_MANAGER,),                 lambda t, ctx: None)

    def passive_observe(self, t: Telegram, ctx: Context) -> None:
        """Track the lock-manager state machine for diagnostics, and run
        the post-wake observation window. Called for every received
        telegram regardless of address."""
        if self._lock_mgr is None:
            self._lock_mgr = LockManager(
                our_address=self.own_address,
                send_fn=ctx.bus.send,
            )
        self._lock_mgr.on_telegram(t)

        # Post-wake observation window: log every RX telegram with a
        # distinctive prefix so we can see what the bus actually does in
        # the seconds after we fire a wake. Only active for a short
        # window after each wake to avoid noise the rest of the time.
        if time.monotonic() < self._wake_watch_until:
            log(f"[wake-watch] RX  to=0x{t.to_addr:02x}  "
                f"from=0x{t.from_addr:02x}  type=0x{t.telegram_type:02x}  "
                f"pt=0x{t.payload_type:02x}  src_dest=0x{t.src_dest:02x}")

    def on_startup(self, ctx: Context) -> None:
        """Mimic a real B&O Source Center's power-on announce sequence.

        Captured BeoSound 5 boot (see ml-startup.txt):
          T+0     SC aux 0x02 -> ALL  TT_CONFIG  PT=08  pl_len=05
          T+0.003 SC main 0xC2 -> ALL_LK STATUS_INFO  source=PC activity=Unknown
          T+0.005 SC aux 0x02 -> VM   MASTER_PRESENT REQ  payload=04 0a 01
          T+0.007 SC aux 0x02 -> AM   MASTER_PRESENT REQ  payload=04 0a 01

        Without this, VM/AM don't have us in their "known devices" list
        and our subsequent telegrams (wake / source-claim / metadata) are
        treated as out-of-band -- accepted in lenient bus states but
        sometimes interpreted as malformed and putting the bus into a
        degraded state until power-cycled. Confirmed by the user's
        observation that "after a power cycle it works again."
        """
        log("[sc] startup -- announcing presence on the bus (real-SC mimic)")
        bus = ctx.bus

        # 1. Aux 0x02 TT_CONFIG announce: "device 0x02 here, source=PC"
        bus.send(B.sc_boot_aux_config())
        time.sleep(0.05)

        # 2. SC main STATUS_INFO with source=PC, activity=Unknown.
        # Uses the boot-specific builder (1 byte longer than active form
        # to match real-SC capture byte-for-byte).
        bus.send(B.sc_boot_status_info_pc())
        time.sleep(0.05)

        # 3. Aux 0x02 -> VM master_present probe
        bus.send(B.sc_aux_master_present(to=ADDR_VM))
        time.sleep(0.05)

        # 4. Aux 0x02 -> AM master_present probe
        bus.send(B.sc_aux_master_present(to=ADDR_AM))

        log("[sc] boot announce complete")

    # ---- handlers ----------------------------------------------------------

    def _on_dist_request(self, t: Telegram, ctx: Context) -> None:
        # DIST_REQUEST's src_dest is the source byte the AM is requesting.
        requested = t.src_dest
        provider = ctx.provider_for(requested)
        if provider is None:
            log(f"[sc] DIST_REQUEST 0x{requested:02x} -- not ours "
                f"(claimed: {[hex(s) for s in ctx.claimed_sources()]}); "
                f"ignoring")
            return
        # Always notify any wake-retry waiter -- the wake has clearly
        # reached the system, regardless of whether we'll actually run
        # the handshake (dedup may suppress it below).
        ack = self._wake_acks.get(requested)
        if ack is not None:
            ack.set()
        # Dedup: skip a duplicate handshake if we just ran one. This
        # protects against the synthetic-+-real DIST_REQUEST race when
        # we self-trigger via _wake_with_retries() and a real one
        # arrives moments later from the actual AM.
        now = time.monotonic()
        last = self._last_handshake_at.get(requested, 0.0)
        if (now - last) < self._DIST_REQUEST_DEDUP_S:
            log(f"[sc] DIST_REQUEST 0x{requested:02x} from "
                f"0x{t.from_addr:02x} -- handshake already ran "
                f"{now - last:.2f}s ago, skipping (dedup)")
            return
        self._last_handshake_at[requested] = now
        log(f"[sc] DIST_REQUEST 0x{requested:02x} from 0x{t.from_addr:02x}"
            f" -- claim handshake (provider={provider.display_name!r})")
        send = ctx.bus.send

        # Single-active-source rule: any other provider currently streaming
        # gets paused before the new one takes over. Pause first, before
        # the handshake, so the audio output is silent by the time the
        # speakers route to us.
        ctx.pause_other_providers(provider)

        # Handshake order matches a captured real B&O Source Center
        # claim (T+0 grant, then settle, then DISPLAY_SOURCE pair, then
        # the full ext_info 1-6 sequence, then a final DISPLAY_SOURCE
        # refresh). Cadence is also from the capture (DISPLAY_SOURCE
        # twice with a ~200ms gap; STATUS / TRACK_INFO / DISPLAY tight).
        send(B.sc_distribution_grant(to=t.from_addr))
        time.sleep(0.20)
        send(B.sc_status_info(source_byte=requested))
        time.sleep(0.005)
        send(B.sc_track_info_long(source_byte=requested))
        time.sleep(0.005)
        # DISPLAY_SOURCE *pair* -- sequence byte flips 1 -> 2. The real
        # SC sends both within ~200ms; some receivers need the second to
        # commit the source name to the front panel.
        send(B.sc_display_source(source_byte=requested,
                                 source_name=provider.display_name,
                                 sequence=1))
        time.sleep(0.20)
        send(B.sc_display_source(source_byte=requested,
                                 source_name=provider.display_name,
                                 sequence=2))
        time.sleep(0.005)
        # Initial metadata burst -- subtype 1 binary blob first, then
        # 2..6 with placeholders. The metadata pump replaces these as
        # soon as the provider reports real data.
        from providers.base import Metadata
        self._push_metadata_set(ctx, provider, Metadata(
            title="Connecting", artist="", album="", genre="",
        ), final_display_source=True)

        provider.play()

    def _on_release(self, t: Telegram, ctx: Context) -> None:
        # RELEASE's src_dest tells us which source is being released.
        # If src_dest=0, the AM is releasing whatever was active -- pause
        # all our providers (cheap, idempotent).
        provider = ctx.provider_for(t.src_dest)
        if provider is not None:
            log(f"[sc] RELEASE 0x{t.src_dest:02x} from 0x{t.from_addr:02x}"
                f" -- pausing {provider.display_name!r}")
            provider.pause()
        elif t.src_dest == 0:
            log(f"[sc] generic RELEASE from 0x{t.from_addr:02x}"
                f" -- pausing all providers")
            for p in ctx.providers.values():
                p.pause()

    def _on_standby(self, t: Telegram, ctx: Context) -> None:
        # Same payload-shape as RELEASE: src_dest is the source byte
        # going to standby. Observed flow when the user changes source
        # from N.MUSIC (us) to a video source on the remote:
        #   VM -> AM   PT=STANDBY  payload=03 03 01 00 01
        #   VM -> SC   PT=DIST_REQUEST  src=0x47 PC  (claim new source)
        #   AM -> SC   PT=STANDBY  src_dest=0x7A (N.MUSIC)  pl_len=0
        # The last one is what reaches us. Without handling it our
        # provider would keep streaming silently (and waste cycles).
        # If src_dest=0, treat it like a generic standby and pause
        # everything.
        provider = ctx.provider_for(t.src_dest)
        if provider is not None:
            log(f"[sc] STANDBY 0x{t.src_dest:02x} from 0x{t.from_addr:02x}"
                f" -- pausing {provider.display_name!r}")
            provider.pause()
        elif t.src_dest == 0:
            log(f"[sc] generic STANDBY from 0x{t.from_addr:02x}"
                f" -- pausing all providers")
            for p in ctx.providers.values():
                p.pause()

    def _on_master_present(self, t: Telegram, ctx: Context) -> None:
        """Respond when VM/AM probe one of our addresses with a
        MASTER_PRESENT REQUEST.

        Real BeoSound 5 (SC) responds to such probes -- without it,
        the probing master times us out and may exclude us from later
        operations. We respond from whichever of our addresses was
        targeted (0xC2 main or 0x02 aux); broadcasts to ALL/ALL_LINK
        get no unsolicited reply.

        Captured probe shapes (real B&O):
          c1 c0 01 0b 00 00 00 04 03 04 02 01     VM->AM REQUEST
          02 c0 01 14 00 00 00 04 03 04 02 01 01  VM->0x02 RESPONSE
          02 c1 01 14 00 00 00 04 03 04 01 02 01  AM->0x02 RESPONSE
        We mimic the response shape (pl_len=03 with 1 byte trailing).
        """
        if t.telegram_type != TT_REQUEST:
            return                       # only answer REQUESTs
        # Don't reply to broadcasts -- only when specifically targeted.
        if t.to_addr == ADDR_SC:
            frm = ADDR_SC
        elif t.to_addr == ADDR_SC_AUX:
            frm = ADDR_SC_AUX
        else:
            return
        log(f"[sc] MASTER_PRESENT? from 0x{t.from_addr:02x} -> "
            f"us 0x{t.to_addr:02x}; responding from 0x{frm:02x}")
        ctx.bus.send(B.sc_master_present_response(
            to=t.from_addr, frm=frm))

    def _on_next(self, t: Telegram, ctx: Context) -> None:
        provider = ctx.provider_for(t.src_dest)
        if provider is None:
            return
        log(f"[sc] BEO4 next on 0x{t.src_dest:02x} "
            f"-> {provider.display_name!r}")
        provider.next()

    def _on_prev(self, t: Telegram, ctx: Context) -> None:
        provider = ctx.provider_for(t.src_dest)
        if provider is None:
            return
        log(f"[sc] BEO4 prev on 0x{t.src_dest:02x} "
            f"-> {provider.display_name!r}")
        provider.prev()

    # ---- provider events ---------------------------------------------------

    def on_provider_event(self, ctx: Context, event: str,
                          provider: object) -> None:
        """React to one provider's lifecycle events. With multiple
        providers registered the same role instance handles events from
        each — `provider` tells us which source the event applies to.
        """
        if event == "stream_started":
            # Single-active-source: pause any other provider that was
            # still streaming before we wake the bus to this one.
            ctx.pause_other_providers(provider)
            # Wake the system, with retries -- the music system can be
            # unresponsive for a few seconds after a prior shutdown
            # sequence, and the wake gets ignored if we send it during
            # that window. Retry up to a few times until we see a
            # DIST_REQUEST come back (set by _on_dist_request).
            import threading
            threading.Thread(
                target=self._wake_with_retries,
                args=(ctx, provider),
                name=f"sc-wake-0x{provider.source_byte:02x}",
                daemon=True,
            ).start()
            return
        if event == "stream_stopped":
            log(f"[sc] stream stopped on 0x{provider.source_byte:02x} "
                f"({provider.display_name!r}) -- broadcasting RELEASE")
            ctx.bus.send(B.release(frm=ADDR_SC))
            return
        if event == "metadata":
            md = provider.metadata()
            if md is None:
                return
            self._push_metadata_set(ctx, provider, md)

    def _push_metadata_set(self, ctx: Context, provider: object,
                           md: object, *,
                           final_display_source: bool = False) -> None:
        """Emit the full N.RADIO-style subtype 1-6 sequence in order.

        The real B&O Source Center sends EXTENDED_SOURCE_INFO with
        subtypes 1, 2, 3, 4, 5, 6 in ascending order on every metadata
        cycle. Subtype 1 is a 22-byte binary blob ("metadata session
        start" marker); 2-6 are text fields. The receiver requires the
        *complete* sequence -- sending only a subset is silently ignored
        on the front panel.

        Subtype meanings for source 0xA1 (N.RADIO) per const.py:
            1: (binary session-start blob -- semantics unknown)
            2: Genre
            3: Country
            4: RDS info  -- the "main" rolling text on the front panel
            5: Associated Beo4 button
            6: "Unknown"  (free-text catch-all)

        If `final_display_source=True`, also emit one final DISPLAY_SOURCE
        right after the sequence. The real SC does this to "commit" the
        metadata to the panel; useful at end-of-handshake.
        """
        src = provider.source_byte

        # Subtype 1 first -- the binary "metadata session start" blob.
        ctx.bus.send(B.sc_extended_source_info_blob(source_byte=src))
        time.sleep(0.005)

        # Map our metadata fields onto the text subtype slots. Empty /
        # unknown fields get a stable placeholder so the receiver still
        # sees the full sequence (just as the real SC sends "NONE" /
        # "Unknown" placeholders rather than skipping subtypes).
        sequence = [
            (0x02, md.genre  or "Network"),    # genre
            (0x03, md.album  or ""),           # country / album slot
            (0x04, md.title  or "Now Playing"),# RDS info / main text
            (0x05, "NONE"),                    # associated Beo4 button
            (0x06, md.artist or "Unknown"),    # "unknown" / artist slot
        ]
        for sub, text in sequence:
            ctx.bus.send(B.sc_extended_source_info(
                source_byte=src, subtype=sub, text=text))
            time.sleep(0.2)

        if final_display_source:
            time.sleep(0.005)
            ctx.bus.send(B.sc_display_source(
                source_byte=src,
                source_name=provider.display_name,
                sequence=1))

    def _send_wake(self, ctx: Context, provider: object) -> None:
        """Inject a virtual Beo4 keypress so the topmost master switches to
        our source. Target depends on bus topology:

          - VM (0xC0) seen recently  -> wake_via_vm (VM orchestrates from there)
          - VM not seen, AM seen     -> wake_via_am
          - neither seen yet         -> broadcast

        The wake telegram bytes mimic exactly what a real AM emits when the
        user presses N.Radio on the remote (captured: c0 c1 01 0a 00 00 00
        20 05 02 00 01 00 00 93). Only the FROM byte differs because we
        identify as SC (0xC2).
        """
        src = provider.source_byte
        beo4 = BEO4_KEY_FOR_SOURCE.get(src)
        if beo4 is None:
            log(f"[sc] no Beo4 key mapped for source 0x{src:02x}; "
                f"can't auto-wake (use auto_wake=false to silence)")
            return

        # Target selection. A real B&O Source Center always targets the
        # VM (capture confirmed). We mirror that as the default. Broadcast
        # wake (TO=0x80) was tried and empirically does NOT trigger the
        # system, so we never fall back to it -- if VM hasn't been heard
        # yet (cold boot), still target VM, since on a typical B&O bus
        # with PWR.DET=5V the master providing power is almost always VM.
        # Only deviate to AM if AM has been heard but VM definitely has
        # not -- characteristic of an AM-standalone setup.
        vm_seen = ctx.topology.seen_recently(ADDR_VM, within_s=60.0)
        am_seen = ctx.topology.seen_recently(ADDR_AM, within_s=60.0)

        if am_seen and not vm_seen:
            log(f"[sc] stream started -- AM-only topology, waking AM (0xC1) "
                f"with Beo4 0x{beo4:02x}")
            ctx.bus.send(B.wake_via_am(frm=ADDR_SC, key=beo4))
            return

        why = "VM seen" if vm_seen else "fresh boot, assuming VM topology"
        log(f"[sc] stream started -- waking VM (0xC0) "
            f"with Beo4 0x{beo4:02x} ({why}, FROM spoofed as AM)")
        ctx.bus.send(B.wake_via_vm(key=beo4))

    def _wake_with_retries(self, ctx: Context, provider: object) -> None:
        """Mirror the captured BS5-SC source-activation flow: TX one
        wake virtual_beo4 to VM (FROM=AM-spoof, PC-form bytes), then
        wait for the real AM to autonomously send DIST_REQUEST to us.

        Captured BS5-SC timeline (ml-startup.txt 2026-05-01 23:44:47):
            T+0       SC TX  wake virtual_beo4   TO=VM FROM=AM-spoof
            T+2ms     VM TX  REQUEST_KEY         broadcast
            T+5ms     AM TX  TRANSFER_KEY        TO=VM
            T+7ms     VM TX  KEY_RECEIVED        TO=AM
            T+11ms    VM TX  MASTER_PRESENT      TO=AM
            T+13ms    AM TX  MASTER_PRESENT      TO=VM (resp)
            T+19ms    VM TX  GOTO_SOURCE         TO=AM   <-- VM commits!
            ... more MP exchanges ...
            T+800ms   AM TX  DIST_REQUEST        TO=SC   <-- our cue
            T+800ms+  SC TX  grant + STATUS_INFO + ...

        VM committing (T+19 ms GOTO_SOURCE) is what powers up VM's
        amp/speakers. The DIST_REQUEST at T+800 ms is just AM asking
        the SC for its audio stream once VM has settled.

        Implementation: TX the captured BS5-SC wake bytes verbatim,
        wait for the real DIST_REQUEST. _on_dist_request sets `ack`,
        which unblocks this loop. If no DIST_REQUEST in the timeout
        window, retry (the bus may have been busy with unrelated
        traffic when the first wake landed).
        """
        import threading
        src = provider.source_byte
        beo4 = BEO4_KEY_FOR_SOURCE.get(src)
        if beo4 is None:
            log(f"[sc] no Beo4 key for 0x{src:02x}; can't auto-wake")
            return

        with self._wake_acks_lock:
            ack = self._wake_acks.get(src)
            if ack is None:
                ack = threading.Event()
                self._wake_acks[src] = ack
        ack.clear()

        for attempt in range(1, self._WAKE_MAX_ATTEMPTS + 1):
            # Bail if the stream stopped mid-retry (user paused / network
            # blip / etc.) -- no point waking for a now-silent provider.
            if not provider.is_playing():
                log(f"[sc] wake for 0x{src:02x} aborted on attempt "
                    f"{attempt}: provider stream stopped")
                return

            # NB: SC does NOT acquire the lock-manager key. That's a
            # VM<->AM affair (confirmed in BS5 capture).

            log(f"[sc] wake attempt {attempt}/{self._WAKE_MAX_ATTEMPTS} "
                f"for 0x{src:02x} -- BS5-SC wake (TO=VM FROM=AM-spoof, "
                f"PC-form), waiting up to {self._WAKE_RETRY_TIMEOUT_S}s "
                f"for AM to autonomously DIST_REQUEST")
            # Arm the wake-watch window so passive_observe logs every
            # RX telegram with a [wake-watch] prefix for the next few
            # seconds. Lets us see the VM/AM lock-manager dance and
            # GOTO_SOURCE land in real time.
            self._wake_watch_until = (
                time.monotonic() + self._WAKE_WATCH_SECONDS)

            # Single TX: the captured BS5-SC wake byte-for-byte. The
            # PC-form (orig_src=0x47, ff ff markers) is what BS5-SC
            # emits in the capture. After this lands, the real VM and
            # AM autonomously dance through their lock + commit cycle,
            # ending with AM sending a real DIST_REQUEST to us.
            ctx.bus.send(B.wake_via_vm(key=beo4))

            if ack.wait(timeout=self._WAKE_RETRY_TIMEOUT_S):
                log(f"[sc] wake for 0x{src:02x} acked after attempt "
                    f"{attempt} (real AM sent DIST_REQUEST)")
                return

        log(f"[sc] wake for 0x{src:02x} unacknowledged after "
            f"{self._WAKE_MAX_ATTEMPTS} attempts -- giving up. "
            f"System may be in deep standby or doesn't accept "
            f"software wake telegrams in this state.",
            err=True)
