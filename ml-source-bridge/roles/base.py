"""Role abstract base class.

A role embodies "which device on the ML bus we're pretending to be." It
owns one ML address (0xC1 for AM, 0xC2 for SC), defines which TO bytes
should be considered ours, and registers the per-payload-type handlers
that respond to incoming requests.

Roles also know how to start/stop a provider and what telegrams to send
on each lifecycle transition (e.g. SC sends a 7-message handshake when
its source is granted; AM sends a 3-message status broadcast).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Set

from core.dispatcher import Context, Dispatcher, HandlerFn, HandlerKey
from core.telegram import Telegram


class Role(ABC):
    own_address: int
    address_aliases: Set[int]
    name: str

    def matches_us(self, t: Telegram) -> bool:
        """True if this telegram's TO field is one of ours (specific address
        or one of the broadcast aliases like ALL / ALL_LINK)."""
        return t.to_addr in self.address_aliases

    @abstractmethod
    def install_handlers(self, d: Dispatcher) -> None:
        """Register all payload-type -> handler mappings for this role."""
        ...

    def on_startup(self, ctx: Context) -> None:
        """Called once after the bridge is fully initialised but before
        the listen loop starts. Default: no-op. Subclasses can override
        to emit a boot announcement (the SC role does this -- see
        captured real-SC behaviour at power-up)."""
        pass

    def passive_observe(self, t: Telegram, ctx: Context) -> None:
        """Called by the main listen loop for EVERY received telegram,
        before role.matches_us() filtering and before dispatch. Subclasses
        can override to track bus-wide state -- e.g. who currently holds
        the lock-manager key, even when those telegrams are addressed to
        other devices.

        Default: no-op.
        """
        pass

    def on_provider_event(self, ctx: Context, event: str,
                          provider: object) -> None:
        """Called when one of the registered providers transitions states.
        Subclasses override to broadcast appropriate telegrams. The
        `provider` arg identifies which source the event applies to (we
        can have several providers active at once).

        Events used:
          - 'stream_started' : provider's audio stream just opened
          - 'stream_stopped' : provider's audio stream just closed
          - 'metadata'       : title/album/artist changed
        """
        pass


# ----------------------------------------------------------------------------

def _register(d: Dispatcher, items: Iterable[tuple]) -> None:
    """Convenience: items is an iterable of (key, fn) pairs."""
    for key, fn in items:
        d.register(key, fn)
