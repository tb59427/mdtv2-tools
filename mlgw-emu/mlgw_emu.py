#!/usr/bin/env python3
"""mlgw-emu -- the Pi as a B&O MasterLink Gateway (MLGW) for Home Assistant.

Home Assistant's mlgw integration (github.com/giachello/mlgw) talks to the
MLGW over three network interfaces. This daemon provides them on the Pi,
backed by the MDT HAT's view of the ML bus (redis link:ml:*), so the
unmodified integration can use the Pi instead of the B&O box:

  HTTP  :80    GET /mlgwpservices.json (Digest or Basic auth) -- rooms,
               devices (MLN) and their sources, in the MLGW's own format.
  TCP   :9000  the documented MLGW protocol: login, ping, serial number,
               Beo4 / BeoRemote One commands to a device (by MLN).
  Telnet :23   the undocumented CLI; "_MLLOG ONLINE" streams every ML
               telegram -- what HA turns into mlgw.ML_telegram events.

Device config: the MLGW's own mlgwpservices.json (download it from the real
MLGW, or write it by hand) plus a map MLN -> ML bus address in [mlgw] --
the MLGW keeps that mapping internally; the integration only learns it by
sending each device a harmless "Light Timeout".

listen_only = true (the default): commands from HA are logged and echoed
into the ML log as if sent, but never put on the bus. Lets the emulation
run next to a real MLGW without both acting on the same command.

Stage 1 of the emulation: no MLGW events yet (source / picture & sound
status, LIGHT/CONTROL, all standby), no virtual buttons, no XMPP/zeroconf
discovery (add the integration manually by IP).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

DEFAULT_CONFIG = "/etc/ml-source-bridge.toml"
CHAN_ML_RX = "link:ml:receive"
CHAN_ML_TX = "link:ml:transmit"

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
MT_REQUEST_SERIAL = 0x39
MT_SERIAL = 0x3A


def log(msg: str) -> None:
    print(f"[mlgw-emu] {msg}", flush=True)


# ---- config -----------------------------------------------------------------

@dataclass
class Config:
    services: dict                      # parsed mlgwpservices.json
    addresses: dict[int, int]           # MLN -> ML bus address
    username: str
    password: str
    listen_only: bool = True
    http_port: int = 80
    telnet_port: int = 23
    api_port: int = 9000
    redis_host: str = "localhost"
    redis_port: int = 6379
    serial: str = ""

    def products(self):
        for zone in self.services.get("zones", []):
            for product in zone.get("products", []):
                yield zone, product


def load_config(path: str) -> Optional[Config]:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    sec = cfg.get("mlgw") or {}
    if not sec.get("enabled", False):
        return None
    json_path = sec.get("config_json", "/etc/mlgw/mlgwpservices.json")
    with open(json_path, encoding="utf-8") as f:
        services = json.load(f)
    addresses = {int(k): int(v) for k, v in (sec.get("addresses") or {}).items()}
    password = str(sec.get("password", ""))
    if not password:
        raise SystemExit("[mlgw] needs a password (HA logs in with it)")
    c = Config(
        services=services, addresses=addresses,
        username=str(sec.get("username", "admin")), password=password,
        listen_only=bool(sec.get("listen_only", True)),
        http_port=int(sec.get("http_port", 80)),
        telnet_port=int(sec.get("telnet_port", 23)),
        api_port=int(sec.get("api_port", services.get("port", 9000))),
        redis_host=str(cfg.get("redis_host", "localhost")),
        redis_port=int(cfg.get("redis_port", 6379)),
        serial=str(sec.get("serial") or services.get("sn", "")),
    )
    # HA connects to whatever port the JSON names; keep them consistent.
    c.services["port"] = c.api_port
    if c.serial:
        c.services["sn"] = c.serial
    for zone, product in c.products():
        mln = product.get("MLN")
        if mln not in c.addresses:
            log(f"WARNING: MLN {mln} ({product.get('name')!r}, {zone.get('name')!r}) "
                f"has no ML address in [mlgw.addresses] -- commands to it are dropped")
    return c


# ---- ML telegrams -------------------------------------------------------------

def beo4_telegram(to: int, cmd: int, dest: int, sec_source: int = 0,
                  link: int = 0) -> bytes:
    """A Beo4 key from the MLGW to a device, as a real MLGW puts it on the
    bus (captured: c0 f0 01 0a 00 00 00 20 05 02 00 01 00 00 58 3b 00 for
    Light Timeout to the video master)."""
    t = bytearray([to, ADDR_MLGW, 0x01, 0x0A, 0x00, 0x00, 0x00,
                   PT_VIRTUAL_BEO4, 0x05, 0x02, 0x00, dest & 0xFF,
                   sec_source & 0xFF, link & 0xFF, cmd & 0xFF])
    t.append(sum(t) & 0xFF)
    t.append(0x00)
    return bytes(t)


def mllog_line(telegram: bytes) -> str:
    """One line of the MLGW CLI's _MLLOG output."""
    stamp = datetime.now().strftime("%Y%m%d-%H:%M:%S:%f:")
    return stamp + " " + " ".join(f"{b:02X}." for b in telegram) + "\r\n"


class TelegramFeed:
    """Fans ML telegrams out to every telnet client in _MLLOG mode."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queues: set[asyncio.Queue] = set()

    def publish(self, telegram: bytes) -> None:          # any thread
        self.loop.call_soon_threadsafe(self._fan_out, telegram)

    def _fan_out(self, telegram: bytes) -> None:
        for q in list(self.queues):
            if q.qsize() < 1000:                          # slow client: drop
                q.put_nowait(telegram)


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
                        feed.publish(bytes.fromhex(m["data"].strip()))
                    except ValueError:
                        pass
        except Exception as e:                            # redis down etc.
            log(f"redis: {e} -- retrying in 2 s")
            stop.wait(2.0)


# ---- HTTP: /mlgwpservices.json ------------------------------------------------

class DigestAuth:
    realm = "MLGW"

    def __init__(self, user: str, password: str) -> None:
        self.user, self.password = user, password
        self.nonces: dict[str, float] = {}

    def challenge(self) -> str:
        nonce = secrets.token_hex(16)
        self.nonces[nonce] = time.monotonic()
        for n, t in list(self.nonces.items()):            # keep it small
            if time.monotonic() - t > 300:
                del self.nonces[n]
        return (f'Digest realm="{self.realm}", qop="auth", nonce="{nonce}", '
                f'opaque="{secrets.token_hex(8)}", algorithm=MD5')

    def check(self, method: str, header: str) -> bool:
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            return user == self.user and pw == self.password
        if not header.startswith("Digest "):
            return False
        p = {m[0]: m[2] if m[2] else m[1]
             for m in re.findall(r'(\w+)=("([^"]*)"|[^,\s]*)', header[7:])}
        if p.get("username") != self.user or p.get("nonce") not in self.nonces:
            return False
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        ha1 = md5(f"{self.user}:{self.realm}:{self.password}")
        ha2 = md5(f"{method}:{p.get('uri', '')}")
        if p.get("qop"):
            want = md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p['qop']}:{ha2}")
        else:
            want = md5(f"{ha1}:{p['nonce']}:{ha2}")
        return secrets.compare_digest(want, p.get("response", ""))


async def handle_http(cfg: Config, auth: DigestAuth,
                      reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    lines = head.decode("latin-1").split("\r\n")
    method, path = (lines[0].split() + ["", ""])[:2]
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()

    def respond(code: str, body: bytes, ctype: str = "text/plain",
                extra: str = "") -> None:
        writer.write((f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\n"
                      f"Content-Length: {len(body)}\r\n{extra}"
                      f"Connection: close\r\n\r\n").encode() + body)

    if path.split("?")[0] != "/mlgwpservices.json":
        respond("404 Not Found", b"not found\n")
    elif not auth.check(method, headers.get("authorization", "")):
        respond("401 Unauthorized", b"unauthorized\n",
                extra=f"WWW-Authenticate: {auth.challenge()}\r\n")
    else:
        log(f"config requested by {writer.get_extra_info('peername')[0]}")
        respond("200 OK", json.dumps(cfg.services).encode(), "application/json")
    try:
        await writer.drain()
    finally:
        writer.close()


# ---- TCP 9000: MLGW protocol --------------------------------------------------

def frame(msg_type: int, payload: bytes = b"") -> bytes:
    return bytes([0x01, msg_type, len(payload), 0x00]) + payload


@dataclass
class ApiSession:
    authed: bool = False
    failures: int = 0
    out: list = field(default_factory=list)


def handle_api_message(cfg: Config, feed: TelegramFeed, s: ApiSession,
                       msg_type: int, payload: bytes, send_to_bus) -> None:
    """One MLGW-protocol message; replies are appended to s.out."""
    if msg_type == MT_PING:
        # The integration pings right after connecting; an unauthenticated
        # ping is answered with "login FAIL", which makes it log in.
        s.out.append(frame(MT_PONG) if s.authed else frame(MT_LOGIN_STATUS, b"\x01"))
    elif msg_type == MT_LOGIN_REQUEST:
        user, _, pw = payload.partition(b"\x00")
        if user.decode(errors="replace") == cfg.username and \
                pw.decode(errors="replace") == cfg.password:
            s.authed = True
            log("HA logged in")
            s.out.append(frame(MT_LOGIN_STATUS, b"\x00"))
        else:
            s.failures += 1
            log(f"login failed for user {user!r}")
            s.out.append(frame(MT_LOGIN_STATUS, b"\x01"))
    elif not s.authed:
        s.out.append(frame(MT_LOGIN_STATUS, b"\x01"))
    elif msg_type == MT_REQUEST_SERIAL:
        s.out.append(frame(MT_SERIAL, cfg.serial.encode()))
    elif msg_type == MT_BEO4_CMD and len(payload) >= 3:
        mln, dest, cmd = payload[0], payload[1], payload[2]
        sec = payload[3] if len(payload) > 3 else 0
        link = payload[4] if len(payload) > 4 else 0
        addr = cfg.addresses.get(mln)
        if addr is None:
            log(f"Beo4 0x{cmd:02x} for unknown MLN {mln} -- dropped")
            return
        t = beo4_telegram(addr, cmd, dest, sec, link)
        if cfg.listen_only:
            log(f"listen-only: Beo4 0x{cmd:02x} dest 0x{dest:02x} -> MLN {mln} "
                f"(0x{addr:02x}) not sent")
            feed.publish(t)                 # as the ML log would show it
        else:
            send_to_bus(t)
    elif msg_type in (MT_BR1_CMD, MT_BR1_SELECT):
        log(f"BeoRemote One message 0x{msg_type:02x} {payload.hex()} -- "
            f"not supported yet")
    elif msg_type == MT_VIRTUAL_BUTTON:
        log(f"virtual button {payload.hex()} -- no macros on the Pi, ignored")
    else:
        log(f"unhandled MLGW message 0x{msg_type:02x} {payload.hex()}")


async def handle_api(cfg: Config, feed: TelegramFeed, send_to_bus,
                     reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    log(f"MLGW protocol connection from {peer}")
    s = ApiSession()
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
                handle_api_message(cfg, feed, s, msg_type, payload, send_to_bus)
            if buf and buf[0] != 0x01:                    # resync on garbage
                buf = buf[buf.find(b"\x01"):] if b"\x01" in buf else b""
            # The integration reads one message per recv(): send replies
            # one by one, never coalesced into a single segment.
            for msg in s.out:
                writer.write(msg)
                await writer.drain()
                await asyncio.sleep(0.05)
            s.out.clear()
            if s.failures >= 3:
                log("too many failed logins -- closing")
                break
    except ConnectionError:
        pass
    finally:
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
        if queue is not None:
            feed.queues.discard(queue)
        if pump is not None:
            pump.cancel()
        writer.close()
        log(f"telnet connection from {peer} closed")


# ---- main -------------------------------------------------------------------------

async def serve(cfg: Config) -> None:
    loop = asyncio.get_running_loop()
    feed = TelegramFeed(loop)
    stop = threading.Event()
    threading.Thread(target=redis_reader, args=(cfg, feed, stop),
                     name="redis", daemon=True).start()

    tx = None

    def send_to_bus(t: bytes) -> None:
        nonlocal tx
        import redis
        if tx is None:
            tx = redis.StrictRedis(host=cfg.redis_host, port=cfg.redis_port)
        tx.publish(CHAN_ML_TX, t.hex())

    auth = DigestAuth(cfg.username, cfg.password)
    servers = [
        await asyncio.start_server(
            lambda r, w: handle_http(cfg, auth, r, w), port=cfg.http_port),
        await asyncio.start_server(
            lambda r, w: handle_api(cfg, feed, send_to_bus, r, w), port=cfg.api_port),
        await asyncio.start_server(
            lambda r, w: handle_telnet(cfg, feed, r, w), port=cfg.telnet_port),
    ]
    n = sum(1 for _ in cfg.products())
    log(f"serving {n} devices: http :{cfg.http_port}, MLGW protocol "
        f":{cfg.api_port}, telnet :{cfg.telnet_port}"
        + (" -- LISTEN-ONLY (commands are not sent to the bus)"
           if cfg.listen_only else ""))
    try:
        await asyncio.gather(*(s.serve_forever() for s in servers))
    finally:
        stop.set()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()
    cfg = load_config(args.config)
    if cfg is None:
        log(f"[mlgw] not enabled in {args.config} -- nothing to do")
        return 0
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
