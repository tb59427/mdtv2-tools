"""The HAT's ADC (PCM1862) is one capture device: only one process can
record from it at a time. Two users inside the bridge:

  * the turntable provider -- ADC (VIN4, phono) -> DAC loopback while the
    record plays;
  * the bus listener (core/ml_listen.py) -- ADC (VIN1, the ML audio lines)
    for music recognition on sources the Pi doesn't provide itself.

The turntable has priority: when it starts its loopback, the listener
stops capturing first; when the turntable stops, the listener may take
the ADC again. Each sets the ADC input mux to its own input when it
starts.
"""
from __future__ import annotations

import threading
from typing import Optional, Protocol


class _Listener(Protocol):
    def suspend(self) -> None: ...      # stop capturing, return once the ADC is free
    def resume(self) -> None: ...       # may capture again


_lock = threading.Lock()
_listener: Optional[_Listener] = None
_turntable = False


def register_listener(listener: Optional[_Listener]) -> None:
    global _listener
    with _lock:
        _listener = listener


def turntable_active() -> bool:
    with _lock:
        return _turntable


def turntable_start() -> None:
    """Before the turntable opens the ADC: blocks until the listener has
    let go of it."""
    global _turntable
    with _lock:
        _turntable = True
        listener = _listener
    if listener is not None:
        listener.suspend()


def turntable_stop() -> None:
    global _turntable
    with _lock:
        _turntable = False
        listener = _listener
    if listener is not None:
        listener.resume()
