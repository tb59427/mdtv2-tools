#!/usr/bin/env python3
"""
ml-broker v2  --  drop-in replacement for ml-broker.py targeting the mdt v2
hardware (ATtiny826 on the HAT takes over the 9-bit MasterLink framing).

The redis interface is identical to v1:
  - subscribe    link:ml:transmit   (publisher sends hex-encoded telegram body
                                     without checksum / 0x00 end-marker, same
                                     as v1)
  - publish      link:ml:receive    (full telegram incl. checksum + 0x00, same
                                     as v1)

Wire protocol to the ATtiny: see mcu-firmware/docs/WIRE_PROTOCOL.md
  [0x55][0xAA][CHAN][LEN][PAYLOAD...][CRC16 little-endian]
  CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over CHAN+LEN+PAYLOAD
"""
from __future__ import annotations

import argparse
import datetime
import sys
import threading
import time

import redis
import serial


_LOG_FILE = "/tmp/mdt.log"
_log_lock = threading.Lock()


def log(msg: str, *, err: bool = False) -> None:
    """Print a timestamped log line + append to the shared mdtv2 log file.

    Output goes to:
      - stdout (or stderr if `err=True`) -- so journald captures it
      - /tmp/mdt.log -- shared with ml-source-bridge so a single
        `tail -F /tmp/mdt.log` shows everything in chronological order

    File writes are guarded by a lock; cross-process atomicity relies on
    POSIX-guaranteed atomic `O_APPEND` writes for lines under PIPE_BUF
    (4 KiB). Our log lines are far shorter than that.
    """
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    level = "WARN " if err else "INFO "
    line = f"{ts} {level} [broker] {msg}"
    print(line, file=sys.stderr if err else sys.stdout, flush=True)
    try:
        with _log_lock:
            with open(_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        # Don't let log-file failure crash the broker.
        pass

# ---- wire protocol constants (keep in sync with src/wire.h) ----
SYNC = bytes([0x55, 0xAA])
CHAN_ML    = 0x00
CHAN_DL86  = 0x11
CHAN_DL80  = 0x12
CHAN_GPIO  = 0x20
CHAN_LOG   = 0xFE
CHAN_PING  = 0xFF
MAX_PAYLOAD = 128   # bumped 64 -> 128 in 2026-05 alongside firmware
                    # v1.5.4. Must stay ≤ ML_MAX_TELEGRAM in the firmware
                    # (mcu-firmware/src/ml.h) since the firmware allocates
                    # tx_buf of that size.

REDIS_ML_TX      = "link:ml:transmit"
REDIS_ML_RX      = "link:ml:receive"
REDIS_PL_TX      = "link:pl:transmit"
REDIS_PL_RX      = "link:pl:receive"
REDIS_IR_TX      = "link:ir:transmit"
REDIS_IR_RX      = "link:ir:receive"
REDIS_DL86_TX    = "link:dl86:transmit"
REDIS_DL86_RX    = "link:dl86:receive"
REDIS_DL80_TX    = "link:dl80:transmit"
REDIS_DL80_RX    = "link:dl80:receive"
REDIS_GPIO_TX    = "link:gpio:transmit"   # set/read board pins (PA4..PA7)
REDIS_GPIO_RX    = "link:gpio:receive"    # state-byte updates from firmware


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(chan: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long: {len(payload)} > {MAX_PAYLOAD}")
    hdr = bytes([chan, len(payload)])
    crc = crc16_ccitt(hdr + payload)
    return SYNC + hdr + payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


class Framer:
    """Byte-by-byte state machine for inbound wire frames."""
    S_SYNC1, S_SYNC2, S_CHAN, S_LEN, S_PAYLOAD, S_CRC_LO, S_CRC_HI = range(7)

    def __init__(self, on_frame):
        self._on_frame = on_frame
        self._reset()

    def _reset(self) -> None:
        self._state = self.S_SYNC1
        self._chan = 0
        self._len = 0
        self._payload = bytearray()
        self._crc_lo = 0

    def feed(self, chunk: bytes) -> None:
        for b in chunk:
            self._feed_byte(b)

    def _feed_byte(self, b: int) -> None:
        s = self._state
        if s == self.S_SYNC1:
            if b == 0x55:
                self._state = self.S_SYNC2
        elif s == self.S_SYNC2:
            if b == 0xAA:
                self._state = self.S_CHAN
            elif b != 0x55:
                self._state = self.S_SYNC1
        elif s == self.S_CHAN:
            self._chan = b
            self._state = self.S_LEN
        elif s == self.S_LEN:
            self._len = b
            self._payload = bytearray()
            if b > MAX_PAYLOAD:
                self._reset()
                return
            self._state = self.S_CRC_LO if b == 0 else self.S_PAYLOAD
        elif s == self.S_PAYLOAD:
            self._payload.append(b)
            if len(self._payload) == self._len:
                self._state = self.S_CRC_LO
        elif s == self.S_CRC_LO:
            self._crc_lo = b
            self._state = self.S_CRC_HI
        elif s == self.S_CRC_HI:
            crc_rx = self._crc_lo | (b << 8)
            crc_calc = crc16_ccitt(bytes([self._chan, self._len]) + bytes(self._payload))
            if crc_rx == crc_calc:
                try:
                    self._on_frame(self._chan, bytes(self._payload))
                except Exception as e:
                    log(f"[broker] on_frame error: {e}", err=True)
            else:
                log(f"[broker] CRC mismatch chan=0x{self._chan:02x} len={self._len}", err=True)
            self._reset()


class Broker:
    def __init__(self, port: str, baud: int, redis_host: str, redis_port: int):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        self.r = redis.StrictRedis(host=redis_host, port=redis_port, db=0)
        self.framer = Framer(self._on_wire_frame)
        self.write_lock = threading.Lock()
        self.last_sent_hex: str | None = None
        self.stop = threading.Event()

    # Known firmware capability bits (mirror of FW_CAP_* in version.h)
    _CAP_NAMES = [(0x01, "ML"), (0x02, "DL86"), (0x04, "DL80"), (0x08, "GPIO")]

    # CHAN_GPIO state-byte bit names (mirror of GPIO_BIT_* in main.c)
    _GPIO_BITS = [
        (0x01, "PWR_EN"),    # PA4 output -- 1 = we drive 5V on ML PWR+
        (0x02, "PWR_DET"),   # PA5 input  -- 1 = external 5V detected
        (0x04, "SL_MA"),     # PA6 output -- 1 = master mode
        (0x08, "DAC_PLAY"),  # PA7 input  -- 1 = on-board DAC playing
    ]

    _ML_MODE_NAMES = {0x00: "slave", 0x01: "master"}

    def _handle_pong(self, payload: bytes) -> None:
        """Parse the ATtiny's PONG reply.

        Layout: "PONG" + major + minor + patch + caps + (since v1.4.0:
        ml_mode byte) + ASCII build_id."""
        if not payload.startswith(b"PONG"):
            log(f"[mdtmcu ping] {payload!r}")
            return
        if len(payload) < 8:
            log(f"[mdtmcu ping] PONG (firmware pre-1.1 -- no version info)")
            return
        major, minor, patch, caps = payload[4], payload[5], payload[6], payload[7]
        caps_list = [name for bit, name in self._CAP_NAMES if caps & bit]
        caps_unknown = caps & ~sum(bit for bit, _ in self._CAP_NAMES)
        if caps_unknown:
            caps_list.append(f"unk=0x{caps_unknown:02x}")
        # Firmware >= 1.4.0 packs an ML mode byte at offset 8 ahead of the
        # ASCII build_id. Detect by version.
        if (major, minor) >= (1, 4):
            ml_mode = payload[8] if len(payload) > 8 else 0xFF
            ml_role = self._ML_MODE_NAMES.get(ml_mode, f"unk=0x{ml_mode:02x}")
            build = payload[9:].decode("ascii", errors="replace").strip()
            extra = f"  ml={ml_role}"
        else:
            build = payload[8:].decode("ascii", errors="replace").strip()
            extra = ""
        if build:
            extra += f"  build={build!r}"
        log(f"[mdtmcu] firmware v{major}.{minor}.{patch}  "
              f"caps=[{','.join(caps_list) or 'none'}]{extra}")

    def _on_wire_frame(self, chan: int, payload: bytes) -> None:
        if chan == CHAN_ML:
            hex_s = payload.hex()
            if self.last_sent_hex and hex_s.startswith(self.last_sent_hex):
                # ATtiny already drops own-TX echoes, but belt & braces.
                return
            log(f"ML RX: {hex_s}")
            self.r.publish(REDIS_ML_RX, hex_s)
        elif chan == CHAN_DL86:
            # payload = [bit_count][packed bits MSB-first]
            if len(payload) < 2:
                log(f"[broker] DL'86 frame too short: {payload.hex()}")
                return
            bit_count = payload[0]
            bits_hex = payload[1:].hex()
            out = f"{bit_count}:{bits_hex}"
            log(f"DL86 RX: {out}")
            self.r.publish(REDIS_DL86_RX, out)
        elif chan == CHAN_DL80:
            # payload = raw bytes (1 byte for single-byte command)
            hex_s = payload.hex()
            log(f"DL80 RX: {hex_s}")
            self.r.publish(REDIS_DL80_RX, hex_s)
        elif chan == CHAN_GPIO:
            self._handle_gpio_state(payload)
        elif chan == CHAN_LOG:
            try:
                msg = payload.decode("utf-8", errors="replace").rstrip()
            except Exception:
                msg = payload.hex()
            log(f"[mdtmcu] {msg}")
        elif chan == CHAN_PING:
            self._handle_pong(payload)
        else:
            log(f"[broker] unknown chan 0x{chan:02x} ({len(payload)} bytes)")

    def send_ml(self, hex_body: str) -> None:
        try:
            body = bytes.fromhex(hex_body)
        except ValueError:
            log(f"[broker] bad hex on {REDIS_ML_TX}: {hex_body!r}", err=True)
            return
        if len(body) > MAX_PAYLOAD:
            log(f"[broker] ML telegram too long: {len(body)} bytes", err=True)
            return
        frame = build_frame(CHAN_ML, body)
        with self.write_lock:
            self.last_sent_hex = hex_body.lower()
            self.ser.write(frame)
        log(f"ML TX: {hex_body}")

    @staticmethod
    def _parse_dl86_input(raw: str) -> "tuple[int, bytes] | None":
        """Accepts any of:
             '10000111100011110'           binary string
             '17:878F00'                   explicit bit_count:hex
             '878F00'                      pure hex -> bit_count = 8*len
        Returns (bit_count, packed_bytes) or None on parse error.
        """
        s = raw.strip().replace(" ", "").replace(".", "")
        if not s:
            return None
        # binary string?
        if set(s) <= {"0", "1"}:
            bit_count = len(s)
            # pad right with zeros to byte boundary, MSB-first
            padded = s + "0" * ((-bit_count) % 8)
            packed = bytes(int(padded[i:i+8], 2) for i in range(0, len(padded), 8))
            return bit_count, packed
        # bit_count:hex?
        if ":" in s:
            a, _, b = s.partition(":")
            try:
                bit_count = int(a)
                packed = bytes.fromhex(b)
            except ValueError:
                return None
            # sanity: packed must cover bit_count bits
            if (bit_count + 7) // 8 > len(packed):
                return None
            return bit_count, packed
        # pure hex
        try:
            packed = bytes.fromhex(s)
        except ValueError:
            return None
        return 8 * len(packed), packed

    def send_dl86(self, raw: str) -> None:
        parsed = self._parse_dl86_input(raw)
        if parsed is None:
            log(f"[broker] bad DL'86 input on {REDIS_DL86_TX}: {raw!r}", err=True)
            return
        bit_count, packed = parsed
        if bit_count == 0 or bit_count > 128:
            log(f"[broker] DL'86 bit_count out of range: {bit_count}", err=True)
            return
        if (1 + len(packed)) > MAX_PAYLOAD:
            log(f"[broker] DL'86 packed too long: {len(packed)} bytes", err=True)
            return
        payload = bytes([bit_count]) + packed
        frame = build_frame(CHAN_DL86, payload)
        with self.write_lock:
            self.ser.write(frame)
        log(f"DL86 TX: {bit_count}:{packed.hex()}")

    def send_dl80(self, raw: str) -> None:
        # Accept hex (optionally with spaces or dots as visual separators) or
        # "0xNN". Single-byte only for now; the firmware handles the repeat.
        s = raw.strip().lower().replace(" ", "").replace(".", "")
        if s.startswith("0x"):
            s = s[2:]
        try:
            body = bytes.fromhex(s)
        except ValueError:
            log(f"[broker] bad DL'80 hex on {REDIS_DL80_TX}: {raw!r}", err=True)
            return
        if len(body) != 1:
            log(f"[broker] DL'80 TX currently single-byte only (got {len(body)} bytes)", err=True)
            return
        frame = build_frame(CHAN_DL80, body)
        with self.write_lock:
            self.ser.write(frame)
        log(f"DL80 TX: {body.hex()}")

    def _handle_gpio_state(self, payload: bytes) -> None:
        """ATtiny -> host CHAN_GPIO frame. Single byte = state bitmap."""
        if len(payload) < 1:
            log(f"[broker] GPIO frame too short: {payload.hex()}")
            return
        s = payload[0]
        names = [n for bit, n in self._GPIO_BITS if s & bit]
        hex_s = f"{s:02x}"
        log(f"GPIO RX: 0x{hex_s}  [{','.join(names) or '-'}]")
        # Publish as hex byte; subscribers can decode bit positions themselves.
        self.r.publish(REDIS_GPIO_RX, hex_s)

    def send_gpio(self, raw: str) -> None:
        """link:gpio:transmit -- empty publish = read-only query (firmware
        responds with current state); otherwise the payload is one hex byte
        which sets the OUTPUT bits (bit 0 = PWR_EN, bit 2 = SL_MA -- input
        bits are ignored). Firmware always responds with the new state."""
        s = raw.strip().lower().replace(" ", "").replace(".", "")
        if s.startswith("0x"):
            s = s[2:]
        if s == "":
            # Read-only: empty payload triggers state report
            frame = build_frame(CHAN_GPIO, b"")
            with self.write_lock:
                self.ser.write(frame)
            log(f"GPIO TX: (read)")
            return
        try:
            body = bytes.fromhex(s)
        except ValueError:
            log(f"[broker] bad GPIO hex on {REDIS_GPIO_TX}: {raw!r}", err=True)
            return
        if len(body) != 1:
            log(f"[broker] GPIO TX expects 1 byte (got {len(body)})", err=True)
            return
        frame = build_frame(CHAN_GPIO, body)
        with self.write_lock:
            self.ser.write(frame)
        log(f"GPIO TX: {body.hex()}")

    def ping(self) -> None:
        with self.write_lock:
            self.ser.write(build_frame(CHAN_PING, b""))

    # ---- threads ----

    def _serial_reader(self) -> None:
        while not self.stop.is_set():
            try:
                data = self.ser.read(256)
            except serial.SerialException as e:
                log(f"[broker] serial read error: {e}", err=True)
                time.sleep(0.5)
                continue
            if data:
                self.framer.feed(data)

    def _redis_subscriber(self) -> None:
        while not self.stop.is_set():
            try:
                pubsub = self.r.pubsub()
                pubsub.subscribe(REDIS_ML_TX, REDIS_DL86_TX, REDIS_DL80_TX,
                                 REDIS_GPIO_TX)
                for m in pubsub.listen():
                    if self.stop.is_set():
                        break
                    if m.get("type") != "message":
                        continue
                    raw = m["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    chan = m.get("channel")
                    if isinstance(chan, bytes):
                        chan = chan.decode("utf-8", errors="replace")
                    if chan == REDIS_ML_TX:
                        self.send_ml(raw.strip())
                    elif chan == REDIS_DL86_TX:
                        self.send_dl86(raw.strip())
                    elif chan == REDIS_DL80_TX:
                        self.send_dl80(raw.strip())
                    elif chan == REDIS_GPIO_TX:
                        self.send_gpio(raw.strip())
            except redis.exceptions.RedisError as e:
                log(f"[broker] redis error: {e}", err=True)
                time.sleep(1.0)

    def run(self) -> None:
        log(f"ml-broker-v2 on {self.ser.port} @ {self.ser.baudrate} baud")
        threading.Thread(target=self._serial_reader, name="serial-reader", daemon=True).start()
        threading.Thread(target=self._redis_subscriber, name="redis-sub", daemon=True).start()
        self.ping()
        try:
            while not self.stop.is_set():
                time.sleep(1.0)
        except KeyboardInterrupt:
            self.stop.set()


def main() -> int:
    ap = argparse.ArgumentParser(description="mdt v2 broker")
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--redis-host", default="localhost")
    ap.add_argument("--redis-port", type=int, default=6379)
    args = ap.parse_args()

    b = Broker(args.port, args.baud, args.redis_host, args.redis_port)
    b.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
