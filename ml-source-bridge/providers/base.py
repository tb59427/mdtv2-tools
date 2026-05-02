"""SourceProvider abstract base class.

A provider is a backend that produces audio for one ML source byte (e.g.
0x7A N.MUSIC, 0x6F RADIO, 0x8D CD). It exposes a uniform interface that
the role layer calls in response to bus events.

Providers are intentionally bus-unaware: they don't construct or send
telegrams. Telegram emission is the role's job.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Metadata:
    """Per-track metadata surfaced over EXTENDED_SOURCE_INFO telegrams.
    Any field may be None / empty -- the role decides what to publish."""
    title: Optional[str] = None     # track title or radio program name
    artist: Optional[str] = None
    album: Optional[str] = None     # album or radio station name
    genre: Optional[str] = None     # MPRIS xesam:genre (first entry)


class SourceProvider(ABC):
    """One audio backend. Lifecycle:
        __init__ -> start (when the bus says we should be active) -> ...
        next/prev/pause/play in response to bus commands -> stop (RELEASE).
    """

    #: ML source byte this provider lives behind (0x7A, 0xA1, 0x6F, 0x8D, …).
    source_byte: int = 0x00

    #: Short printable name shown on B&O displays. Max 12 chars.
    display_name: str = "?"

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Called when the bus has just told us to take over (e.g. AM
        granted a GOTO_SOURCE for our source). Default: no-op."""
        pass

    def stop(self) -> None:
        """Called on RELEASE / global-off. Default: pause."""
        self.pause()

    # ---- transport control --------------------------------------------------

    @abstractmethod
    def play(self) -> None: ...

    @abstractmethod
    def pause(self) -> None: ...

    def next(self) -> None:
        """Step to the next track / station. Default: no-op."""
        pass

    def prev(self) -> None:
        """Step to the previous track / station. Default: no-op."""
        pass

    # ---- introspection ------------------------------------------------------

    @abstractmethod
    def is_playing(self) -> bool: ...

    def metadata(self) -> Optional[Metadata]:
        """Best-effort current metadata. May return None if unknown."""
        return None
