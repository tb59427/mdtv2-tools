#!/usr/bin/env python3
"""mlgw-emu -- the Pi as a B&O MasterLink Gateway (MLGW) for Home Assistant.

Home Assistant's mlgw integration (github.com/giachello/mlgw) talks to the
MLGW over three network interfaces. This daemon provides them on the Pi,
backed by the MDT HAT's view of the ML bus (redis link:ml:*), so the
unmodified integration can use the Pi instead of the B&O box:

  HTTP  :80    GET /mlgwpservices.json -- rooms, devices (MLN), sources;
               served by mdt-web (web/), which also has the UI to edit them.
  TCP   :9000  the documented MLGW protocol: login, ping, serial number,
               Beo4 commands to a device (by MLN); events derived from the
               bus (source status, picture & sound status, LIGHT/CONTROL,
               all standby -- see events.py).
  Telnet :23   the undocumented CLI; "_MLLOG ONLINE" streams every ML
               telegram -- what HA turns into mlgw.ML_telegram events.

Devices: store.py (the MLGW's JSON format plus each device's ML bus
address, which a real MLGW keeps internally), edited in mdt-web. After a
save, mdt-web publishes "mlgw-emu" on redis link:ctl:restart: the devices
are reloaded in place and HA gets the MLGW "configuration changed"
message, so it reloads the integration. Status for the UI (HA connections,
listen-only) goes to redis key state:mlgw.

listen_only = true (the default): commands from HA are logged and echoed
into the ML log as if sent, but never put on the bus -- lets the emulation
run next to a real MLGW. Set it to false once the Pi replaces the MLGW.

Not emulated: virtual buttons / macros, BeoRemote One commands,
XMPP/zeroconf discovery (add the integration manually by IP).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from events import EventSynth                         # noqa: E402
from store import DeviceStore                        # noqa: E402

DEFAULT_CONFIG = "/etc/ml-source-bridge.toml"
CHAN_ML_RX = "link:ml:receive"
CHAN_ML_TX = "link:ml:transmit"
CTL_RESTART = "link:ctl:restart"                     # mdt-web: "mlgw-emu" = reload devices
STATE_KEY = "state:mlgw"                             # status for mdt-web

ADDR_MLGW = 0xF0
PT_VIRTUAL_BEO4 = 0x20

# MLGW protocol message types (MlgwProto0240)
MT_BEO4_CMD = 0x01
MT_BR1_CMD = 0x06
MT_BR1_SELECT = 0x07
MT_VIRTUAL_BUTTON = 0x20
MT_LOGIN_REQUEST = 0x30
MT_LOGIN_STATUS = 0x31
MT_PING = 0x36
MT_PONG = 0x37
MT_CONFIG_CHANGED = 0x38
MT_REQUEST_SERIAL = 0x39
MT_SERIAL = 0x3A


def log(msg: str) -> None:
    print(f"[mlgw-emu] {msg}", flush=True)


# ---- config -----------------------------------------------------------------

@dataclass
class Config:
    username: str
    password: str
    devices_file: str
    listen_only: bool = True
    telnet_port: int = 23
    api_port: int = 9000
    redis_host: str = "localhost"
    redis_port: int = 6379
    serial: str = ""
    extended_status: bool = False       # source status for link rooms too
    seed_json: str = ""                 # MLGW export to start from
    seed_addresses: dict = field(default_factory=dict)


def load_config(path: str) -> Optional[Config]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    sec = cfg.get("mlgw") or {}
    if not sec.get("enabled", False):
        return None
    password = str(sec.get("password", ""))
    if not password:
        raise SystemExit("[mlgw] needs a password (HA and the web UI log in with it)")
    return Config(
        username=str(sec.get("username", "admin")), password=password,
        devices_file=str(sec.get("devices_file", "/var/lib/mdt-mlgw/devices.json")),
        listen_only=bool(sec.get("listen_only", True)),
        telnet_port=int(sec.get("telnet_port", 23)),
        api_port=int(sec.get("api_port", 9000)),
        redis_host=str(cfg.get("redis_host", "localhost")),
        redis_port=int(cfg.get("redis_port", 6379)),
        serial=str(sec.get("serial", "")),
        extended_status=bool(sec.get("extended_status", False)),
        seed_json=str(sec.get("config_json", "")),
        seed_addresses={int(k): int(v) for k, v in (sec.get("addresses") or {}).items()},
    )


def open_store(cfg: Config) -> DeviceStore:
    """The device file; on first start seeded from config_json (a real
    MLGW's export) and [mlgw.addresses], if given."""
    store = DeviceStore(cfg.devices_file)
    if store.load():
        return store
    if cfg.seed_json:
        with open(cfg.seed_json, encoding="utf-8") as f:
            store.seed(json.load(f), cfg.seed_addresses)
        store.save(store.data)
        log(f"created {cfg.devices_file} from {cfg.seed_json}")
    else:
        log(f"{cfg.devices_file} doesn't exist yet -- set up devices in the web UI")
    return store


# ---- ML telegrams -------------------------------------------------------------

def beo4_telegram(to: int, cmd: int, dest: int, sec_source: int = 0,
                  link: int = 0) -> bytes:
    """A Beo4 key from the MLGW to a device, as a real MLGW puts it on the
    bus (captured: c0 f0 01 0a 00 00 00 20 05 02 00 01 00 00 58 [3b 00] for
    Light Timeout to the video master). Without checksum and end marker:
    that's what the broker takes on link:ml:transmit (it adds both) and
    what the MLGW's _MLLOG shows."""
    return bytes([to, ADDR_MLGW, 0x01, 0x0A, 0x00, 0x00, 0x00,
                  PT_VIRTUAL_BEO4, 0x05, 0x02, 0x00, dest & 0xFF,
                  sec_source & 0xFF, link & 0xFF, cmd & 0xFF])


def mllog_line(telegram: bytes) -> str:
    """One line of the MLGW CLI's _MLLOG output."""
    stamp = datetime.now().strftime("%Y%m%d-%H:%M:%S:%f:")
    return stamp + " " + " ".join(f"{b:02X}." for b in telegram) + "\r\n"


class TelegramFeed:
    """Every ML telegram goes to the telnet clients in _MLLOG mode and to
    the event synthesizer -- all on the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop,
                 synth: Optional[EventSynth] = None) -> None:
        self.loop = loop
        self.synth = synth
        self.queues: set[asyncio.Queue] = set()

    def publish(self, telegram: bytes) -> None:          # any thread
        self.loop.call_soon_threadsafe(self._fan_out, telegram)

    def _fan_out(self, telegram: bytes) -> None:
        for q in list(self.queues):
            if q.qsize() < 1000:                          # slow client: drop
                q.put_nowait(telegram)
        if self.synth is not None:
            self.synth.feed(telegram, time.time())


def redis_reader(cfg: Config, feed: TelegramFeed, stop: threading.Event) -> None:
    """Thread: everything on the bus (received, and what the Pi itself
    sends) goes to the feed -- the real MLGW sees both on the wire."""
    import redis
    while not stop.is_set():
        try:
            r = redis.StrictRedis(host=cfg.redis_host, port=cfg.redis_port,
                                  decode_responses=True)
            ps = r.pubsub(ignore_subscribe_messages=True)
            ps.subscribe(CHAN_ML_RX, CHAN_ML_TX)
            log("following the ML bus on redis")
            while not stop.is_set():
                m = ps.get_message(timeout=1.0)
                if m and isinstance(m.get("data"), str):
                    try:
                        t = bytes.fromhex(m["data"].strip())
                    except ValueError:
                        continue
                    # rx carries checksum + 0x00 end marker, tx doesn't;
                    # the MLGW's _MLLOG shows neither.
                    if m["channel"] == CHAN_ML_RX and len(t) > 2:
                        t = t[:-2]
                    feed.publish(t)
        except Exception as e:                            # redis down etc.
            log(f"redis: {e} -- retrying in 2 s")
            stop.wait(2.0)


# ---- outgoing telegrams -------------------------------------------------------

class Pacer:
    """Puts our telegrams out one at a time, `gap_s` apart, like a real
    MLGW on the bus. Matters beyond bus etiquette: the integration learns
    device addresses by matching the ML log's answers to its commands IN
    ORDER, and HA handles events on several threads -- a burst of answers
    arrives shuffled and devices get each other's addresses.

    listen_only: the telegram only goes into the ML log (as if sent);
    otherwise onto the bus, and the bus echo reaches the log by itself."""

    def __init__(self, cfg: Config, feed: TelegramFeed, gap_s: float = 0.15) -> None:
        self.cfg, self.feed, self.gap_s = cfg, feed, gap_s
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._tx = None

    def emit(self, telegram: bytes) -> None:
        self.queue.put_nowait(telegram)

    def _send_to_bus(self, t: bytes) -> None:
        import redis
        if self._tx is None:
            self._tx = redis.StrictRedis(host=self.cfg.redis_host,
                                         port=self.cfg.redis_port)
        self._tx.publish(CHAN_ML_TX, t.hex())

    async def run(self) -> None:
        while True:
            t = await self.queue.get()
            try:
                if self.cfg.listen_only:
                    self.feed.publish(t)
                else:
                    self._send_to_bus(t)
            except Exception as e:
                log(f"sending {t.hex()} failed: {e}")
            await asyncio.sleep(self.gap_s)


# ---- TCP 9000: MLGW protocol --------------------------------------------------

def frame(msg_type: int, payload: bytes = b"") -> bytes:
    return bytes([0x01, msg_type, len(payload), 0x00]) + payload


class ApiSession:
    """One HA connection. Everything we send goes through `out`, one
    message per TCP write: the integration reads one message per recv()."""

    def __init__(self) -> None:
        self.authed = False
        self.failures = 0
        self.out: asyncio.Queue[bytes] = asyncio.Queue()

    def send(self, msg: bytes) -> None:
        self.out.put_nowait(msg)


class Gateway:
    """Shared state of the running emulation."""

    def __init__(self, cfg: Config, store: DeviceStore) -> None:
        self.cfg = cfg
        self.store = store
        self.sessions: set[ApiSession] = set()
        self.synth = EventSynth(store.by_address, self.broadcast, log,
                                extended=cfg.extended_status)
        self.pacer: Optional[Pacer] = None

    def publish_status(self) -> None:
        """state:mlgw for mdt-web's status display."""
        try:
            import redis
            redis.StrictRedis(host=self.cfg.redis_host, port=self.cfg.redis_port).set(
                STATE_KEY, json.dumps({
                    "sessions": sum(1 for s in self.sessions if s.authed),
                    "listen_only": self.cfg.listen_only,
                    "devices": sum(1 for _ in self.store.products()),
                    "time": time.time()}))
        except Exception as e:
            log(f"status to redis failed: {e}")

    def reload(self) -> None:
        """Devices changed in mdt-web: reload, tell HA to reload too."""
        try:
            self.store.load()
        except Exception as e:
            log(f"reloading {self.cfg.devices_file} failed: {e} -- keeping the old devices")
            return
        log("devices reloaded -- telling HA to reload the integration")
        self.broadcast(frame(MT_CONFIG_CHANGED))
        self.publish_status()

    def broadcast(self, msg: bytes) -> None:
        """An MLGW event to every logged-in HA connection."""
        for s in list(self.sessions):
            if s.authed:
                s.send(msg)

    def serial(self) -> str:
        return self.cfg.serial or str(self.store.data.get("sn", ""))

    def handle(self, s: ApiSession, msg_type: int, payload: bytes) -> None:
        if msg_type == MT_PING:
            # The integration pings right after connecting; an unauthenticated
            # ping is answered with "login FAIL", which makes it log in.
            s.send(frame(MT_PONG) if s.authed else frame(MT_LOGIN_STATUS, b"\x01"))
        elif msg_type == MT_LOGIN_REQUEST:
            user, _, pw = payload.partition(b"\x00")
            if user.decode(errors="replace") == self.cfg.username and \
                    pw.decode(errors="replace") == self.cfg.password:
                s.authed = True
                log("HA logged in")
                s.send(frame(MT_LOGIN_STATUS, b"\x00"))
                self.publish_status()
            else:
                s.failures += 1
                log(f"login failed for user {user!r}")
                s.send(frame(MT_LOGIN_STATUS, b"\x01"))
        elif not s.authed:
            s.send(frame(MT_LOGIN_STATUS, b"\x01"))
        elif msg_type == MT_REQUEST_SERIAL:
            s.send(frame(MT_SERIAL, self.serial().encode()))
        elif msg_type == MT_BEO4_CMD and len(payload) >= 3:
            mln, dest, cmd = payload[0], payload[1], payload[2]
            sec = payload[3] if len(payload) > 3 else 0
            link = payload[4] if len(payload) > 4 else 0
            addr = self.store.addresses().get(mln)
            if addr is None:
                log(f"Beo4 0x{cmd:02x} for MLN {mln} without ML address -- dropped")
                return
            if self.cfg.listen_only:
                log(f"listen-only: Beo4 0x{cmd:02x} dest 0x{dest:02x} -> MLN {mln} "
                    f"(0x{addr:02x}) not sent")
            self.pacer.emit(beo4_telegram(addr, cmd, dest, sec, link))
        elif msg_type in (MT_BR1_CMD, MT_BR1_SELECT):
            log(f"BeoRemote One message 0x{msg_type:02x} {payload.hex()} -- "
                f"not supported yet")
        elif msg_type == MT_VIRTUAL_BUTTON:
            log(f"virtual button {payload.hex()} -- no macros on the Pi, ignored")
        else:
            log(f"unhandled MLGW message 0x{msg_type:02x} {payload.hex()}")


async def handle_api(gw: Gateway, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    log(f"MLGW protocol connection from {peer}")
    s = ApiSession()
    gw.sessions.add(s)

    async def pump() -> None:
        while True:
            msg = await s.out.get()
            writer.write(msg)
            await writer.drain()
            await asyncio.sleep(0.05)               # never coalesce two messages
    sender = asyncio.create_task(pump())
    buf = b""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf += data
            while len(buf) >= 4 and buf[0] == 0x01 and len(buf) >= 4 + buf[2]:
                n = buf[2]
                msg_type, payload, buf = buf[1], buf[4:4 + n], buf[4 + n:]
                gw.handle(s, msg_type, payload)
            if buf and buf[0] != 0x01:              # resync on garbage
                buf = buf[buf.find(b"\x01"):] if b"\x01" in buf else b""
            if s.failures >= 3:
                log("too many failed logins -- closing")
                break
    except ConnectionError:
        pass
    finally:
        gw.sessions.discard(s)
        gw.publish_status()
        sender.cancel()
        writer.close()
        log(f"MLGW protocol connection from {peer} closed")


# ---- Telnet :23: CLI with _MLLOG ------------------------------------------------

def strip_telnet(data: bytes) -> bytes:
    """Drop telnet option negotiation (IAC sequences) from client input."""
    out, i = bytearray(), 0
    while i < len(data):
        b = data[i]
        if b != 0xFF:
            out.append(b)
            i += 1
        elif i + 1 < len(data) and data[i + 1] == 0xFA:   # subnegotiation
            end = data.find(b"\xff\xf0", i)
            i = len(data) if end < 0 else end + 2
        else:
            i += 3
    return bytes(out)


# Telnet sessions by client IP. When HA reloads the integration, the old
# instance's session can linger for a moment and deliver ML-log lines to
# HA twice -- the new instance then counts one address-learning answer too
# many (devices shifted by one, or an exception in the integration). A new
# login from the same host therefore closes that host's older sessions.
TELNET_SESSIONS: dict[str, set[asyncio.StreamWriter]] = {}


async def handle_telnet(cfg: Config, feed: TelegramFeed,
                        reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    queue: Optional[asyncio.Queue] = None
    pump: Optional[asyncio.Task] = None

    async def readline() -> Optional[str]:
        line = b""
        while not line.endswith(b"\n"):
            chunk = await reader.read(256)
            if not chunk:
                return None
            line += strip_telnet(chunk)
        return line.decode(errors="replace").strip()

    async def stream() -> None:
        while True:
            t = await queue.get()
            writer.write(mllog_line(t).encode())
            await writer.drain()

    try:
        writer.write(b"login: ")
        await writer.drain()
        pw = await asyncio.wait_for(readline(), 30)
        if pw != cfg.password:
            writer.write(b"\r\nLogin incorrect\r\n")
            await writer.drain()
            log(f"telnet login failed from {peer}")
            return
        for old in list(TELNET_SESSIONS.get(peer, ())):
            log(f"closing older telnet session from {peer}")
            old.close()
        TELNET_SESSIONS.setdefault(peer, set()).add(writer)
        writer.write(b"\r\nmdtv2 MLGW emulation\r\nMLGW >")
        await writer.drain()
        log(f"telnet login from {peer}")
        while True:
            cmd = await readline()
            if cmd is None:
                break
            if cmd.upper() == "_MLLOG ONLINE" and queue is None:
                queue = asyncio.Queue()
                feed.queues.add(queue)
                pump = asyncio.create_task(stream())
                log(f"ML log streaming to {peer}")
            elif cmd and queue is None:
                writer.write(b"\r\nMLGW >")
                await writer.drain()
    except (asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        TELNET_SESSIONS.get(peer, set()).discard(writer)
        if queue is not None:
            feed.queues.discard(queue)
        if pump is not None:
            pump.cancel()
        writer.close()
        log(f"telnet connection from {peer} closed")


# ---- main -------------------------------------------------------------------------

def wait_for_restart(host: str, port: int) -> None:
    import redis
    while True:
        try:
            ps = redis.StrictRedis(host=host, port=port, decode_responses=True
                                   ).pubsub(ignore_subscribe_messages=True)
            ps.subscribe(CTL_RESTART)
            while True:
                m = ps.get_message(timeout=5.0)
                if m and m.get("data") in ("mlgw-emu", "mlgw-emu:restart", "all"):
                    log("restart requested (config changed)")
                    return
        except Exception:
            time.sleep(2.0)


def ctl_listener(cfg: Config, loop: asyncio.AbstractEventLoop, gw: "Gateway",
                 stop: threading.Event) -> None:
    """Thread: mdt-web saved the devices -> reload them on the loop."""
    import redis
    while not stop.is_set():
        try:
            ps = redis.StrictRedis(host=cfg.redis_host, port=cfg.redis_port,
                                   decode_responses=True).pubsub(
                ignore_subscribe_messages=True)
            ps.subscribe(CTL_RESTART)
            while not stop.is_set():
                m = ps.get_message(timeout=1.0)
                data = m.get("data") if m else None
                if data == "mlgw-emu":
                    loop.call_soon_threadsafe(gw.reload)
                elif data in ("mlgw-emu:restart", "all"):
                    # [mlgw] settings changed (listen_only, password, ...):
                    # exit, systemd (Restart=always) starts us with them.
                    log("restart requested (config changed) -- exiting")
                    os._exit(0)
        except Exception:
            stop.wait(2.0)


async def serve(cfg: Config) -> None:
    store = open_store(cfg)
    gw = Gateway(cfg, store)
    loop = asyncio.get_running_loop()
    feed = TelegramFeed(loop, gw.synth)
    stop = threading.Event()
    threading.Thread(target=redis_reader, args=(cfg, feed, stop),
                     name="redis", daemon=True).start()
    threading.Thread(target=ctl_listener, args=(cfg, loop, gw, stop),
                     name="ctl", daemon=True).start()
    gw.pacer = Pacer(cfg, feed)
    pacer_task = asyncio.create_task(gw.pacer.run())
    gw.publish_status()
    servers = [
        await asyncio.start_server(lambda r, w: handle_api(gw, r, w), port=cfg.api_port),
        await asyncio.start_server(
            lambda r, w: handle_telnet(cfg, feed, r, w), port=cfg.telnet_port),
    ]
    n = sum(1 for _ in store.products())
    log(f"serving {n} devices: MLGW protocol :{cfg.api_port}, "
        f"telnet :{cfg.telnet_port} (config + UI: mdt-web)"
        + (" -- LISTEN-ONLY (commands are not sent to the bus)"
           if cfg.listen_only else ""))
    for _z, p in store.products():
        if not isinstance(p.get("mlAddress"), int):
            log(f"WARNING: MLN {p.get('MLN')} ({p.get('name')!r}) has no ML address")
    try:
        await asyncio.gather(*(s.serve_forever() for s in servers))
    finally:
        pacer_task.cancel()
        stop.set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if cfg is None:
        # Idle until mdt-web switches the emulation on (exiting here would
        # make systemd's Restart=always loop).
        log(f"[mlgw] not enabled in {args.config} -- idle until the config changes")
        with open(args.config, "rb") as f:
            raw = tomllib.load(f)
        wait_for_restart(str(raw.get("redis_host", "localhost")),
                         int(raw.get("redis_port", 6379)))
        return 0
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
