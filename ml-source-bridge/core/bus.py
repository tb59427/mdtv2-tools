"""Redis pubsub plumbing for ML telegrams.

Provides:
  - Bus: send / receive ML telegrams via the broker's redis channels, with
    a clean polling listen loop (Ctrl-C exits within 0.5s, no hung sockets)
  - detect_firmware_role: query the firmware's PA6 (master/slave) pin via
    a CHAN_GPIO round-trip. Used as a sanity check against configured role.
"""
from __future__ import annotations

import datetime
import logging
import sys
import threading
import time
from typing import Callable, Iterator, Optional

import redis

from core.telegram import Telegram


# Bit positions in the GPIO state byte the firmware publishes on
# link:gpio:receive (mirror of GPIO_BIT_* in mcu-firmware/src/main.c).
GPIO_BIT_PWR_EN   = 0x01
GPIO_BIT_PWR_DET  = 0x02
GPIO_BIT_SL_MA    = 0x04   # 1 = master mode
GPIO_BIT_DAC_PLAY = 0x08

CHAN_ML_RX   = "link:ml:receive"
CHAN_ML_TX   = "link:ml:transmit"
CHAN_GPIO_RX = "link:gpio:receive"
CHAN_GPIO_TX = "link:gpio:transmit"


_logger = logging.getLogger("msb")


def log(msg: str, *, err: bool = False) -> None:
    """Compatibility shim. Routes legacy `log("[xxx] ...")` calls through
    Python's logging module so they land in both the log file and stderr.
    `err=True` becomes WARNING level; otherwise INFO.
    """
    if err:
        _logger.warning(msg)
    else:
        _logger.info(msg)


# ----------------------------------------------------------------------------

class Bus:
    """Thin wrapper around redis pubsub for ML traffic. One instance per
    process; thread-safe for `send` (redis-py is thread-safe for publish)."""

    def __init__(self, host: str = "localhost", port: int = 6379,
                 stop: Optional[threading.Event] = None) -> None:
        self.r = redis.StrictRedis(host=host, port=port, db=0,
                                   socket_keepalive=True)
        self.stop = stop or threading.Event()

    # -- send -----------------------------------------------------------------

    def send(self, telegram_bytes: bytes) -> None:
        """Publish one ML telegram (without checksum / 0x00 end marker --
        broker adds those)."""
        hex_s = telegram_bytes.hex()
        self.r.publish(CHAN_ML_TX, hex_s)

    # -- receive --------------------------------------------------------------

    def listen_ml(self) -> Iterator[Telegram]:
        """Yield each ML RX telegram until the stop event is set.

        Uses get_message(timeout=) so the stop flag is checked between
        polls -- signal handlers don't need to do anything except set
        the event.
        """
        pubsub: Optional[redis.client.PubSub] = None
        try:
            while not self.stop.is_set():
                try:
                    if pubsub is None:
                        pubsub = self.r.pubsub()
                        pubsub.subscribe(CHAN_ML_RX)
                    m = pubsub.get_message(timeout=0.5,
                                           ignore_subscribe_messages=True)
                    if m is None:
                        continue
                    data = m.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8", errors="replace")
                    try:
                        yield Telegram.from_hex(data)
                    except ValueError:
                        log(f"[bus] bad hex on {CHAN_ML_RX}: {data!r}", err=True)
                except redis.exceptions.RedisError as e:
                    log(f"[bus] redis error: {e}", err=True)
                    if pubsub is not None:
                        try: pubsub.close()
                        except Exception: pass
                        pubsub = None
                    self.stop.wait(1.0)
        finally:
            if pubsub is not None:
                try: pubsub.close()
                except Exception: pass

    # -- close ----------------------------------------------------------------

    def close(self) -> None:
        try:
            self.r.close()
        except Exception:
            pass


# ----------------------------------------------------------------------------
#
# Topology / role detection.
#
# Role itself is configured statically (config.toml or --role flag); the
# only thing we read from hardware is the firmware's PWR.DET pin via
# CHAN_GPIO. That tells us whether external 5V is on the bus, which is
# useful as a sanity check against the configured role.
#
# Active probing of the bus turned out empirically unreliable -- VM
# responds to a spoofed-AM LOCK_MGR REQUEST_KEY broadcast, but AM is
# silent in cold idle no matter what we send. So we don't try; the role
# comes from config and topology fills in passively as masters chatter.


def detect_firmware_role(bus: Bus, timeout: float = 2.0) -> Optional[str]:
    """Query the firmware once to see whether it's currently in master or
    slave mode. Returns 'am' (master), 'sc' (slave), or None on timeout.

    Mechanism: subscribe link:gpio:receive, publish empty payload to
    link:gpio:transmit (the broker forwards this as a CHAN_GPIO read-only
    query), wait for the firmware's state byte. Bit 2 (SL_MA) tells us
    the role.
    """
    pubsub = bus.r.pubsub()
    try:
        pubsub.subscribe(CHAN_GPIO_RX)
        # Drain any pre-existing messages before issuing our query, so we
        # don't latch onto a stale value.
        deadline_drain = time.monotonic() + 0.1
        while time.monotonic() < deadline_drain:
            if pubsub.get_message(timeout=0.05,
                                  ignore_subscribe_messages=True) is None:
                break
        # Issue the query.
        bus.r.publish(CHAN_GPIO_TX, "")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            m = pubsub.get_message(timeout=0.2,
                                   ignore_subscribe_messages=True)
            if m is None:
                continue
            data = m.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            try:
                state = int(data, 16)
            except (TypeError, ValueError):
                continue
            return "am" if (state & GPIO_BIT_SL_MA) else "sc"
        return None
    finally:
        try: pubsub.close()
        except Exception: pass


# ----------------------------------------------------------------------------

class Periodic(threading.Thread):
    """Run `fn()` once every `interval_s` until `stop` is set. First call
    fires after `initial_delay_s`."""

    def __init__(self, fn: Callable[[], None], interval_s: float,
                 stop: threading.Event, initial_delay_s: float = 0.0,
                 name: str = "periodic") -> None:
        super().__init__(name=name, daemon=True)
        self._fn = fn
        self._interval = interval_s
        self._stop = stop
        self._delay = initial_delay_s

    def run(self) -> None:
        if self._stop.wait(self._delay):
            return
        while not self._stop.is_set():
            try:
                self._fn()
            except Exception as e:
                log(f"[{self.name}] error: {e}", err=True)
            if self._stop.wait(self._interval):
                return
