"""MultiSourceProvider -- fan out one ML source byte to several backends.

The rest of the bridge treats each ML source byte as having exactly one
provider (dict `providers[source_byte] -> SourceProvider`). That's the
right model for the bus -- only one audio stream flows at a time -- but
it means N.MUSIC (0x7A) is either AirPlay or Sendspin or MPD, never a
"whichever is currently sending". This wrapper lifts that: it holds a
list of sub-providers and presents them upstream as one SourceProvider.

Semantics:

  is_playing() : True if any sub is playing. On each poll the wrapper
                 also decides which sub is the "active" one. Only ever
                 one sub is considered active at a time (single-audio-
                 path rule). Last-writer-wins: if a new sub starts while
                 another one was already active, the new one becomes
                 active and the old one is paused. The upstream watcher
                 doesn't see an edge -- from its point of view the
                 source stayed playing throughout the handover.

  display_name : dynamically tracks the active sub's configured display
                 name (e.g. "Apple Music" for airplay, "Music Assistant"
                 for sendspin). When nothing is playing, falls back to
                 the source-level display (e.g. "N.MUSIC"). Read as a
                 property so `provider.display_name` always reflects
                 the current active sub without the caller caring.

  metadata()   : from the active sub. None when nothing is playing.

  play()       : if a sub was recently active, replay it. Otherwise the
                 configured default sub. Never fanned out to all subs --
                 waking three of them into a race would be worse than
                 the miss.

  pause()      : fanned out to all subs, defensively. A pause is a
                 "silence, please" command; we want it to reach any sub
                 that might still be pushing audio, not just the one we
                 think is active.

  next() / prev() : only to the active sub. If none is active, no-op.
                    (Skipping a track on an idle backend has no
                    meaningful semantics.)

  start() / stop() : fanned out to all subs. Both are lifecycle-level.
"""
from __future__ import annotations

from typing import Optional

from core.bus import log
from providers.base import Metadata, SourceProvider


class MultiSourceProvider(SourceProvider):
    """Wraps N sub-providers behind one ML source byte."""

    def __init__(
        self,
        source_byte: int,
        fallback_display: str,
        subs: list[SourceProvider],
        sub_displays: list[str],
        default_idx: int = 0,
    ) -> None:
        if not subs:
            raise ValueError("MultiSourceProvider needs at least one sub-provider")
        if len(subs) != len(sub_displays):
            raise ValueError("subs and sub_displays must have the same length")
        if not (0 <= default_idx < len(subs)):
            raise ValueError(f"default_idx {default_idx} out of range")

        self.source_byte = source_byte
        self._fallback_display = fallback_display
        self._subs: list[SourceProvider] = list(subs)
        self._sub_displays: list[str] = list(sub_displays)
        self._default_idx = default_idx
        # Index into _subs of whichever sub last reported is_playing=True
        # (or None if nothing was playing at last check). Updated inside
        # is_playing(); read by display_name / metadata / next / prev.
        self._active_idx: Optional[int] = None

    # ---- display_name is dynamic ------------------------------------------

    @property
    def display_name(self) -> str:                              # type: ignore[override]
        """Follows the currently active sub. When idle, the source-level
        fallback (typical B&O source label like 'N.MUSIC')."""
        if self._active_idx is not None:
            return self._sub_displays[self._active_idx]
        return self._fallback_display

    @display_name.setter
    def display_name(self, _value: str) -> None:
        # Base class type-hints display_name as a plain str attribute
        # settable in subclasses -- swallow assignments here so a stray
        # `provider.display_name = "..."` from elsewhere doesn't crash.
        # The value would be meaningless anyway (we're driven by subs).
        pass

    # ---- lifecycle --------------------------------------------------------

    def start(self) -> None:
        for i, sub in enumerate(self._subs):
            try:
                sub.start()
            except Exception as e:
                log(f"[multi 0x{self.source_byte:02x}] sub "
                    f"{self._sub_displays[i]!r} start failed: {e}", err=True)

    def stop(self) -> None:
        for i, sub in enumerate(self._subs):
            try:
                sub.stop()
            except Exception as e:
                log(f"[multi 0x{self.source_byte:02x}] sub "
                    f"{self._sub_displays[i]!r} stop failed: {e}", err=True)

    # ---- transport --------------------------------------------------------

    def play(self) -> None:
        # If someone was recently active, hand play to them (resume the
        # session they had). Otherwise the configured default sub.
        idx = self._active_idx if self._active_idx is not None else self._default_idx
        self._safe_call(idx, "play")

    def pause(self) -> None:
        # Fan out. A "please be silent" applies to everyone that might
        # still be pushing audio.
        for i in range(len(self._subs)):
            self._safe_call(i, "pause")

    def next(self) -> None:
        if self._active_idx is not None:
            self._safe_call(self._active_idx, "next")

    def prev(self) -> None:
        if self._active_idx is not None:
            self._safe_call(self._active_idx, "prev")

    # ---- introspection ----------------------------------------------------

    def is_playing(self) -> bool:
        """True if any sub is currently playing.

        Side effect: maintains `_active_idx` under a last-writer-wins
        rule. If a NEW sub is playing while another was already active,
        the new one becomes active and the old one is paused (single-
        audio-path rule -- ML can only carry one source).

        Deliberate no-notify: internal sub-switches don't fire a
        stream_started/stopped edge upstream. From the bridge's view
        the source stayed continuously playing; only display_name and
        metadata change. That's what we want -- the ML handshake stays
        valid and the B&O display refreshes to the new sub's name via
        the next metadata push.
        """
        playing = []
        for i, sub in enumerate(self._subs):
            try:
                if sub.is_playing():
                    playing.append(i)
            except Exception as e:
                log(f"[multi 0x{self.source_byte:02x}] sub "
                    f"{self._sub_displays[i]!r} is_playing raised: {e}",
                    err=True)
        if not playing:
            if self._active_idx is not None:
                self._active_idx = None
            return False

        # Someone is playing. Pick the "new arrival" if there is one --
        # that is, any sub that's playing but isn't the current active.
        # Anyone else in `playing` besides that pick gets paused (rare
        # race case where two subs came up between polls).
        newcomers = [i for i in playing if i != self._active_idx]
        if newcomers:
            new_idx = newcomers[0]
            # Pause the previous active (if any) so audio doesn't overlap.
            if self._active_idx is not None:
                log(f"[multi 0x{self.source_byte:02x}] sub switch: "
                    f"{self._sub_displays[self._active_idx]!r} -> "
                    f"{self._sub_displays[new_idx]!r} "
                    f"(last-writer-wins)")
                self._safe_call(self._active_idx, "pause")
            # Extra newcomers (>1 simultaneously) -- pause them too so
            # exactly one sub owns the audio path.
            for extra in newcomers[1:]:
                log(f"[multi 0x{self.source_byte:02x}] extra sub "
                    f"{self._sub_displays[extra]!r} also playing -- "
                    f"pausing to enforce single-source rule")
                self._safe_call(extra, "pause")
            self._active_idx = new_idx
        return True

    def metadata(self) -> Optional[Metadata]:
        if self._active_idx is None:
            return None
        try:
            return self._subs[self._active_idx].metadata()
        except Exception as e:
            log(f"[multi 0x{self.source_byte:02x}] active sub "
                f"{self._sub_displays[self._active_idx]!r} metadata "
                f"raised: {e}", err=True)
            return None

    # ---- private ----------------------------------------------------------

    def _safe_call(self, idx: int, method: str) -> None:
        """Call a method on one sub, swallow (and log) exceptions."""
        try:
            getattr(self._subs[idx], method)()
        except Exception as e:
            log(f"[multi 0x{self.source_byte:02x}] sub "
                f"{self._sub_displays[idx]!r} {method}() raised: {e}",
                err=True)
