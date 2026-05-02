"""Passive bus-topology tracker.

Records the last time a telegram was seen from each ML address. Lets role
logic ask 'is there a VM on the bus?' without active probing.

The SC role uses this to decide where to send its wake virtual-Beo4: a
B&O setup with a Video Master orchestrates source selection through the
VM (0xC0), so SC -> VM is the right path. In a setup with only an AUDIO
MASTER (0xC1), SC -> AM is correct. We pick passively based on which
master we've heard from recently.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional


class Topology:
    def __init__(self) -> None:
        self._last_seen: Dict[int, float] = {}
        self._lock = threading.Lock()

    def saw(self, addr: int) -> None:
        """Mark `addr` as having sent us a telegram just now."""
        with self._lock:
            self._last_seen[addr] = time.monotonic()

    def seen_recently(self, addr: int, within_s: float = 60.0) -> bool:
        """True if we've heard from `addr` within the last `within_s` seconds."""
        with self._lock:
            t = self._last_seen.get(addr)
        return t is not None and (time.monotonic() - t) < within_s

    def last_seen_age(self, addr: int) -> Optional[float]:
        """Seconds since we last heard from `addr`, or None if never."""
        with self._lock:
            t = self._last_seen.get(addr)
        return None if t is None else (time.monotonic() - t)
