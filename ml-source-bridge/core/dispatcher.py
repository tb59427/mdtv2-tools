"""Telegram -> handler dispatch.

A handler is a function of (telegram, context) -> None. It does whatever it
likes (typically: ask the provider to do something, send response telegrams
via context.bus). The dispatcher matches incoming telegrams by a tuple key
that is one of:

    (payload_type,)                       -- match any telegram of that pl_type
    (payload_type, src_dest)              -- match plus a specific src_dest byte
    (payload_type, "key", key_byte)       -- match BEO4_KEY/VIRTUAL_BEO4 with a
                                             specific key code at the right byte

Most-specific match wins.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from core.telegram import (
    PT_BEO4_KEY, PT_VIRTUAL_BEO4, Telegram,
)


# A handler key. Stored as a tuple; we try the longest-prefix match first.
HandlerKey = Tuple


@dataclass
class Context:
    """Passed to every handler. Carries everything the handler needs to
    react: the bus to send replies on, the registered providers (keyed by
    ML source byte), the role (so handlers can look up own_address etc.
    without globals), and a passive topology tracker (so handlers / wake
    logic can decide where to address outgoing telegrams based on who's
    currently on the bus).

    `providers` maps source byte -> provider. Multiple sources are
    supported by registering several providers, e.g.:
        {0x7A: AirPlayProvider, 0xA1: MpdProvider}
    Handlers dispatch on the telegram's `src_dest` field via
    `ctx.provider_for(byte)`.
    """
    bus: object        # core.bus.Bus -- forward-decl to dodge the import cycle
    role: object       # roles.base.Role
    providers: dict    # int (source byte) -> providers.base.SourceProvider
    topology: object   # core.topology.Topology

    def provider_for(self, source_byte: int):
        """Return the provider for `source_byte`, or None if we don't
        claim that source."""
        return self.providers.get(source_byte)

    def claimed_sources(self) -> list:
        """List of source bytes we've registered providers for. Useful
        for log messages / introspection."""
        return sorted(self.providers.keys())

    def pause_other_providers(self, except_for) -> None:
        """Pause every registered provider except `except_for`.

        Enforces the "only one source plays at a time" rule: there's a
        single shared audio output (the on-board DAC) and a single ML
        bus state, so we can never have two providers streaming
        simultaneously. Called whenever a new provider is becoming
        active -- either via the SC handshake responding to a
        DIST_REQUEST from the bus, or via the stream-watcher detecting
        that a backend has started streaming on its own.
        """
        # Local import dodges any import cycle through core.bus.
        from core.bus import log
        for src, p in self.providers.items():
            if p is except_for:
                continue
            try:
                if not p.is_playing():
                    continue
            except Exception:
                continue
            log(f"[coord] pausing {p.display_name!r} (0x{src:02x}) -- "
                f"new active source is {except_for.display_name!r} "
                f"(0x{except_for.source_byte:02x})")
            try:
                p.pause()
            except Exception as e:
                log(f"[coord] error pausing {p.display_name!r}: {e}",
                    err=True)


HandlerFn = Callable[[Telegram, Context], None]


class Dispatcher:
    """Maps incoming Telegrams to handlers. Owns the routing table; the role
    populates it at startup via register()."""

    def __init__(self) -> None:
        self._table: Dict[HandlerKey, HandlerFn] = {}

    def register(self, key: HandlerKey, fn: HandlerFn) -> None:
        self._table[key] = fn

    def dispatch(self, t: Telegram, ctx: Context) -> bool:
        """Look up and invoke the most-specific handler for `t`. Returns
        True if a handler ran, False otherwise."""
        # Build candidate keys ordered most-specific first.
        candidates = []
        pt = t.payload_type
        # Beo4-key telegrams have the key code at a different offset in the
        # payload depending on whether they're BEO4_KEY (0x0D, byte 11) or
        # VIRTUAL_BEO4 (0x20, byte 14). Build a key-based match for both.
        if pt == PT_BEO4_KEY:
            candidates.append((pt, "key", t.at(11)))
        elif pt == PT_VIRTUAL_BEO4:
            candidates.append((pt, "key", t.at(14)))
        # Plain (pl_type, src_dest) and (pl_type,) variants.
        candidates.append((pt, t.src_dest))
        candidates.append((pt,))
        for k in candidates:
            fn = self._table.get(k)
            if fn is not None:
                fn(t, ctx)
                return True
        return False
