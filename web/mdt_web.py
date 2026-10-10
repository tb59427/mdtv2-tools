#!/usr/bin/env python3
"""mdt-web -- web UI for the mdtv2 Pi: configure the bridge and the MLGW
emulation from a browser instead of over ssh.

  /                     the UI (tabs: status, sources, turntable, Home
                        Assistant, LIGHT keys, MasterLink Gateway)
  /mlgwpservices.json   device config for HA's mlgw integration (when the
                        MLGW emulation is on) -- HA expects it on port 80
  /api/...              JSON API used by the UI

Bridge config (/etc/ml-source-bridge.toml) is edited with tomlkit: only
values that changed are written, so comments, layout and settings the UI
doesn't know about stay as they are. A save
  1. builds the new file and has the bridge itself validate it
     (ml_source_bridge.py --check-config),
  2. keeps a backup of the old file (state dir, last 20),
  3. writes the file in place (same inode, owner and mode),
  4. asks the affected services to restart (redis link:ctl:restart) --
     no root needed: they exit and systemd starts them again.

Everyone who can log in can change the LIGHT-key shell commands, which run
as the service user -- protect the login accordingly.

Config: [web] in the same TOML (enabled, port, username, password,
state_dir, units). MLGW device config: see mlgw-emu/store.py.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from typing import Optional

import tomlkit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                         # repo / /opt/mdt-tools
sys.path.insert(0, os.path.join(ROOT, "mlgw-emu"))
from store import ConfigError, DeviceStore           # noqa: E402

BRIDGE = os.path.join(ROOT, "ml-source-bridge", "ml_source_bridge.py")
DEFAULT_CONFIG = "/etc/ml-source-bridge.toml"
CTL_RESTART = "link:ctl:restart"
DEFAULT_UNITS = ["mdtv2-broker", "ml-source-bridge", "mdt-state", "ha-notifier",
                 "mlgw-emu", "mdt-web", "shairport-sync", "sendspin", "mpd"]
PROVIDERS = ["airplay", "sendspin", "mpd", "turntable"]


def log(msg: str) -> None:
    print(f"[mdt-web] {msg}", flush=True)


# ---- auth -------------------------------------------------------------------

class DigestAuth:
    """HTTP Digest (and Basic) auth. Several accounts: the UI login and,
    for /mlgwpservices.json, the MLGW credentials HA uses."""
    realm = "MLGW"

    def __init__(self) -> None:
        self.nonces: dict[str, float] = {}

    def challenge(self) -> str:
        nonce = secrets.token_hex(16)
        self.nonces[nonce] = time.monotonic()
        for n, t in list(self.nonces.items()):
            if time.monotonic() - t > 3600:
                del self.nonces[n]
        return (f'Digest realm="{self.realm}", qop="auth", nonce="{nonce}", '
                f'opaque="{secrets.token_hex(8)}", algorithm=MD5')

    def check(self, method: str, header: str, accounts: dict[str, str]) -> bool:
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            return user in accounts and secrets.compare_digest(accounts[user], pw)
        if not header.startswith("Digest "):
            return False
        p = {m[0]: m[2] if m[2] else m[1]
             for m in re.findall(r'(\w+)=("([^"]*)"|[^,\s]*)', header[7:])}
        user = p.get("username")
        if user not in accounts or p.get("nonce") not in self.nonces:
            return False
        md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
        ha1 = md5(f"{user}:{self.realm}:{accounts[user]}")
        ha2 = md5(f"{method}:{p.get('uri', '')}")
        if p.get("qop"):
            want = md5(f"{ha1}:{p['nonce']}:{p.get('nc', '')}:{p.get('cnonce', '')}:{p['qop']}:{ha2}")
        else:
            want = md5(f"{ha1}:{p['nonce']}:{ha2}")
        return secrets.compare_digest(want, p.get("response", ""))


# ---- bridge config: TOML <-> model --------------------------------------------

def _hexint(value: int):
    """A TOML integer rendered as hex (0xA1), like the example config."""
    return tomlkit.parse(f"x = 0x{value:02X}")["x"]


_NO_DEFAULT = object()


def _set(table, key: str, value, hexfmt: bool = False, default=_NO_DEFAULT) -> None:
    """Write `value` only if it changed -- untouched values keep their
    formatting and comments. A key that isn't in the file is only added
    when the value differs from `default` (what the services assume when
    it's missing). None removes the key."""
    if value is None:
        if key in table:
            del table[key]
        return
    if key in table:
        if table[key] == value:
            return
    elif default is not _NO_DEFAULT and value == default:
        return
    table[key] = _hexint(value) if hexfmt and isinstance(value, int) else value


def _table(doc, key: str):
    if key not in doc:
        doc[key] = tomlkit.table()
    return doc[key]


def toml_to_model(cfg: dict) -> dict:
    """The parts of the bridge config the UI edits."""
    wt = cfg.get("wake_target", "vm")
    tt = cfg.get("turntable") or {}
    hn = cfg.get("ha_notifier") or {}
    lh = cfg.get("light_handler") or {}
    mg = cfg.get("mlgw") or {}
    ml = cfg.get("ml_listen") or {}
    return {
        "role": cfg.get("role", "sc"),
        "broadcast_clock": cfg.get("broadcast_clock", True),
        "auto_wake": cfg.get("auto_wake", True),
        "wake_target": f"0x{wt:02X}" if isinstance(wt, int) else str(wt),
        "sources": [{
            "source_byte": s.get("source_byte"),
            "provider": s.get("provider", "airplay"),
            "provider_default": s.get("provider_default"),
            "display_name": s.get("display_name", ""),
        } for s in cfg.get("sources") or []],
        "provider_displays": dict(cfg.get("provider_displays") or {}),
        "mpd": {"host": (cfg.get("mpd") or {}).get("host", "localhost"),
                "port": (cfg.get("mpd") or {}).get("port", 6600),
                "password": (cfg.get("mpd") or {}).get("password", "")},
        "turntable": {"loopback": tt.get("loopback", True),
                      "metadata_title": tt.get("metadata_title", "PHONO"),
                      "recognize": tt.get("recognize", False),
                      "pga_db": float(tt.get("pga_db", 0.0)),
                      "standby_ml_on_stop": tt.get("standby_ml_on_stop", True)},
        "ml_listen": {"enabled": ml.get("enabled", False),
                      "sources": [str(x) for x in ml.get("sources") or []]},
        "ha_notifier": {"enabled": hn.get("enabled", False),
                        "url": hn.get("url", ""),
                        "sources": list(hn.get("sources") or []),
                        "cover_port": hn.get("cover_port", 8099)},
        "light_handler": {"enabled": lh.get("enabled", False),
                          "timeout_s": lh.get("timeout_s", 20),
                          "commands": {str(k): str(v) for k, v in
                                       (lh.get("commands") or {}).items()}},
        "mlgw": {"enabled": mg.get("enabled", False),
                 "listen_only": mg.get("listen_only", True),
                 "username": mg.get("username", "admin"),
                 "password_set": bool(mg.get("password"))},
    }


def apply_model(doc, m: dict, cfg: dict) -> None:
    """Write the UI model into the tomlkit document in place."""
    _set(doc, "role", m["role"], default="sc")
    _set(doc, "broadcast_clock", bool(m["broadcast_clock"]), default=True)
    _set(doc, "auto_wake", bool(m["auto_wake"]), default=True)
    wt = str(m["wake_target"]).strip()
    _set(doc, "wake_target", int(wt, 16) if wt.lower().startswith("0x") else wt,
         hexfmt=True, default="vm")

    # [[sources]]: update entries in place (keeps their comments), append /
    # drop at the end.
    new = m["sources"]
    if "sources" not in doc:
        doc["sources"] = tomlkit.aot()
    aot = doc["sources"]
    while len(aot) > len(new):
        del aot[len(aot) - 1]
    for i, s in enumerate(new):
        if i >= len(aot):
            aot.append(tomlkit.table())
        t = aot[i]
        prov = s["provider"]
        if isinstance(prov, list) and len(prov) == 1:
            prov = prov[0]
        _set(t, "source_byte", int(s["source_byte"]), hexfmt=True)
        _set(t, "provider", prov)
        _set(t, "provider_default", s.get("provider_default")
             if isinstance(prov, list) else None)
        _set(t, "display_name", s.get("display_name") or None)

    pd = _table(doc, "provider_displays")
    for k in [k for k in pd if k not in m["provider_displays"]]:
        del pd[k]
    for k, v in m["provider_displays"].items():
        _set(pd, k, v)

    uses = {p for s in new for p in (s["provider"] if isinstance(s["provider"], list)
                                     else [s["provider"]])}
    if "mpd" in uses or "mpd" in doc:
        t = _table(doc, "mpd")
        _set(t, "host", m["mpd"]["host"], default="localhost")
        _set(t, "port", int(m["mpd"]["port"]), default=6600)
        _set(t, "password", m["mpd"]["password"], default="")
    if "turntable" in uses or "turntable" in doc:
        t = _table(doc, "turntable")
        for k, d in (("loopback", True), ("recognize", False), ("standby_ml_on_stop", True)):
            _set(t, k, bool(m["turntable"][k]), default=d)
        _set(t, "metadata_title", m["turntable"]["metadata_title"], default="PHONO")
        _set(t, "pga_db", float(m["turntable"]["pga_db"]), default=0.0)

    ml = m["ml_listen"]
    if ml["enabled"] or "ml_listen" in doc:
        t = _table(doc, "ml_listen")
        _set(t, "enabled", bool(ml["enabled"]))
        _set(t, "sources", [str(x).strip() for x in ml["sources"] if str(x).strip()] or None)

    hn = m["ha_notifier"]
    if hn["enabled"] or "ha_notifier" in doc:
        t = _table(doc, "ha_notifier")
        _set(t, "enabled", bool(hn["enabled"]))
        _set(t, "url", hn["url"])
        _set(t, "sources", list(hn["sources"]) or None)
        _set(t, "cover_port", int(hn["cover_port"]), default=8099)

    lh = m["light_handler"]
    if lh["enabled"] or lh["commands"] or "light_handler" in doc:
        t = _table(doc, "light_handler")
        _set(t, "enabled", bool(lh["enabled"]))
        _set(t, "timeout_s", int(lh["timeout_s"]), default=20)
        cmds = _table(t, "commands")
        for k in [k for k in cmds if k not in lh["commands"]]:
            del cmds[k]
        for k, v in lh["commands"].items():
            _set(cmds, k, v)

    mg = m["mlgw"]
    if mg["enabled"] or "mlgw" in doc:
        t = _table(doc, "mlgw")
        _set(t, "enabled", bool(mg["enabled"]))
        _set(t, "listen_only", bool(mg["listen_only"]), default=True)
        _set(t, "username", mg.get("username") or "admin", default="admin")
        if mg.get("password"):                       # write-only in the UI
            _set(t, "password", mg["password"])


def affected_services(old: dict, new: dict) -> list[str]:
    """Which services must restart for a model change."""
    out = []
    bridge_keys = ("role", "broadcast_clock", "auto_wake", "wake_target", "sources",
                   "provider_displays", "mpd", "turntable", "ml_listen", "light_handler")
    if any(old[k] != new[k] for k in bridge_keys):
        out.append("ml-source-bridge")
    if old["ha_notifier"] != new["ha_notifier"]:
        out.append("ha-notifier")
    mo, mn = dict(old["mlgw"]), dict(new["mlgw"])
    mn.pop("password", None)
    mo.pop("password_set", None), mn.pop("password_set", None)
    if mo != mn or new["mlgw"].get("password"):
        out.append("mlgw-emu:restart")
    return out


# ---- the app ------------------------------------------------------------------

class App:
    def __init__(self, config_path: str, raw: dict) -> None:
        self.config_path = config_path
        web = raw.get("web") or {}
        self.port = int(web.get("port", 80))
        self.username = str(web.get("username", "admin"))
        self.password = str(web.get("password", ""))
        if not self.password:
            raise SystemExit("[web] needs a password")
        self.state_dir = str(web.get("state_dir", "/var/lib/mdt-web"))
        self.units = list(web.get("units") or DEFAULT_UNITS)
        self.redis_host = str(raw.get("redis_host", "localhost"))
        self.redis_port = int(raw.get("redis_port", 6379))
        self.auth = DigestAuth()
        self.lock = threading.Lock()
        self.last_key: dict = {}
        with open(os.path.join(HERE, "ui", "index.html"), "rb") as f:
            self.index = f.read()
        self.tables = self._tables()

    # -- helpers --------------------------------------------------------------

    def raw(self) -> dict:
        with open(self.config_path, "rb") as f:
            return tomllib.load(f)

    def redis(self):
        import redis
        return redis.StrictRedis(host=self.redis_host, port=self.redis_port,
                                 decode_responses=True)

    def mlgw_store(self) -> Optional[DeviceStore]:
        mg = self.raw().get("mlgw") or {}
        if not mg.get("enabled"):
            return None
        st = DeviceStore(str(mg.get("devices_file", "/var/lib/mdt-mlgw/devices.json")))
        st.load()
        return st

    def _tables(self) -> dict:
        out = {"keys": {}, "sources": {}, "light_keys": [], "providers": PROVIDERS}
        sys.path.insert(0, os.path.join(ROOT, "ml-debug"))
        try:
            import const as mlconst
            out["keys"] = {str(k): v for k, v in sorted(mlconst.beo4_commanddict.items())}
            out["sources"] = {str(k): v for k, v in sorted(mlconst.ml_selectedsourcedict.items())}
        except Exception as e:
            log(f"ml-debug tables unavailable: {e}")
        try:
            src = open(os.path.join(ROOT, "ml-source-bridge", "core", "light_handler.py")).read()
            block = src[src.index("BEO4_KEY_NAMES"):]
            block = block[:block.index("}")]
            out["light_keys"] = re.findall(r'"([a-z0-9_]+)"\s*:', block)
        except Exception as e:
            log(f"LIGHT key names unavailable: {e}")
        return out

    def publish_restart(self, services: list[str]) -> None:
        r = self.redis()
        for s in services:
            r.publish(CTL_RESTART, s)
        if services:
            log(f"restart requested: {', '.join(services)}")

    def backup(self) -> None:
        d = os.path.join(self.state_dir, "backups")
        os.makedirs(d, exist_ok=True)
        shutil.copy2(self.config_path, os.path.join(
            d, time.strftime("ml-source-bridge-%Y%m%d-%H%M%S.toml")))
        for old in sorted(os.listdir(d))[:-20]:
            os.unlink(os.path.join(d, old))

    def check(self, text: str) -> list[str]:
        """The bridge's own validation of a candidate config."""
        os.makedirs(self.state_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".candidate-", suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            r = subprocess.run([sys.executable, BRIDGE, "--config", tmp, "--check-config",
                                "--log-file", ""],
                               capture_output=True, text=True, timeout=30)
        finally:
            os.unlink(tmp)
        problems = [l for l in r.stdout.splitlines() if l.strip()]
        if r.returncode != 0 and not problems:
            problems = [r.stderr.strip().splitlines()[-1] if r.stderr.strip()
                        else f"check failed (exit {r.returncode})"]
        return problems

    # -- API ------------------------------------------------------------------

    def get_bridge(self) -> dict:
        return toml_to_model(self.raw())

    def put_bridge(self, model: dict) -> dict:
        with self.lock:
            with open(self.config_path, encoding="utf-8") as f:
                text = f.read()
            old = toml_to_model(tomllib.loads(text))
            doc = tomlkit.parse(text)
            apply_model(doc, model, tomllib.loads(text))
            new_text = tomlkit.dumps(doc)
            problems = self.check(new_text)
            mg = model["mlgw"]
            if mg["enabled"] and not (mg.get("password") or old["mlgw"]["password_set"]):
                problems.append("MasterLink Gateway: set a password (HA logs in with it)")
            if model["ha_notifier"]["enabled"] and not str(
                    model["ha_notifier"]["url"]).startswith(("http://", "https://")):
                problems.append("Home Assistant: the webhook URL must start with http://")
            if problems:
                return {"ok": False, "problems": problems}
            if new_text == text:
                return {"ok": True, "changed": False, "restarted": []}
            self.backup()
            with open(self.config_path, "r+", encoding="utf-8") as f:   # same inode
                f.write(new_text)
                f.truncate()
            services = affected_services(old, toml_to_model(tomllib.loads(new_text)))
            self.publish_restart(services)
            log(f"config saved, restarting: {services or 'nothing'}")
            return {"ok": True, "changed": True, "restarted": services}

    def status(self) -> dict:
        units = {}
        for u in self.units:
            try:
                r = subprocess.run(["systemctl", "is-active", u], capture_output=True,
                                   text=True, timeout=5)
                units[u] = r.stdout.strip() or "unknown"
            except Exception:
                units[u] = "unknown"
        out = {"units": units, "nowplaying": {}, "mlgw": None}
        try:
            r = self.redis()
            out["nowplaying"] = {k: json.loads(v) for k, v in
                                 (r.hgetall("state:nowplaying") or {}).items()}
            m = r.get("state:mlgw")
            out["mlgw"] = json.loads(m) if m else None
        except Exception as e:
            out["error"] = f"redis: {e}"
        return out

    def bus_devices(self) -> list:
        try:
            raw = self.redis().get("state:ml:devices")
            devs = (json.loads(raw) or {}).get("devices", {}) if raw else {}
        except Exception:
            return []
        out = []
        for a, d in devs.items():
            try:
                out.append({"address": int(a, 16), "class": d.get("class"),
                            "name": d.get("role"), "present": d.get("present", True)})
            except (ValueError, AttributeError):
                pass
        return sorted(out, key=lambda d: d["address"])

    def key_watcher(self) -> None:
        """Thread: remember the latest Beo4 key on the bus (UI 'identify')."""
        while True:
            try:
                ps = self.redis().pubsub(ignore_subscribe_messages=True)
                ps.subscribe("link:ml:receive")
                while True:
                    m = ps.get_message(timeout=5.0)
                    if not m:
                        continue
                    try:
                        t = bytes.fromhex(m["data"].strip())
                    except ValueError:
                        continue
                    if len(t) >= 12 and t[7] == 0x0D:          # BEO4_KEY
                        self.last_key = {"address": t[1], "source": t[10],
                                         "key": t[11], "time": time.time()}
            except Exception:
                time.sleep(2.0)

    def api(self, method: str, path: str, body: bytes):
        if path == "/api/meta":
            raw = self.raw()
            st = None
            try:
                st = self.mlgw_store()
            except Exception:
                pass
            return 200, {**self.tables, "serial": (st.identity()[0] if st else ""),
                         "mlgw_enabled": bool((raw.get("mlgw") or {}).get("enabled"))}
        if path == "/api/status":
            return 200, self.status()
        if path == "/api/bridge":
            if method == "GET":
                return 200, self.get_bridge()
            if method == "PUT":
                res = self.put_bridge(json.loads(body))
                return (200 if res["ok"] else 400), res
        if path == "/api/bus-devices":
            return 200, self.bus_devices()
        if path == "/api/last-key":
            return 200, self.last_key
        if path.startswith("/api/mlgw/"):
            st = self.mlgw_store()
            if st is None:
                return 409, {"error": "MasterLink Gateway emulation is off"}
            if path == "/api/mlgw/config" and method == "GET":
                return 200, st.data
            if path == "/api/mlgw/config" and method == "PUT":
                try:
                    st.save(json.loads(body))
                except (ConfigError, ValueError) as e:
                    return 400, {"error": str(e)}
                self.publish_restart(["mlgw-emu"])           # reload in place
                return 200, {"ok": True}
            if path == "/api/mlgw/import" and method == "POST":
                try:
                    return 200, st.import_mlgw(json.loads(body))
                except ValueError as e:
                    return 400, {"error": f"not an MLGW export: {e}"}
        return 404, {"error": "unknown API call"}

    # -- HTTP -----------------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, target = (lines[0].split() + ["", ""])[:2]
            headers = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            n = int(headers.get("content-length", "0") or 0)
            if n > 2_000_000:
                raise ValueError("body too large")
            body = await asyncio.wait_for(reader.readexactly(n), 10) if n else b""
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, ConnectionError, ValueError):
            writer.close()
            return

        def respond(code: int, payload: bytes, ctype: str, extra: str = "") -> None:
            reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
                      409: "Conflict", 500: "Internal Server Error"}.get(code, "")
            writer.write((f"HTTP/1.1 {code} {reason}\r\nContent-Type: {ctype}\r\n"
                          f"Content-Length: {len(payload)}\r\nCache-Control: no-store\r\n"
                          f"{extra}Connection: close\r\n\r\n").encode() + payload)

        path = target.split("?")[0]
        accounts = {self.username: self.password}
        try:
            if path == "/mlgwpservices.json":
                raw = self.raw()
                mg = raw.get("mlgw") or {}
                if mg.get("password"):
                    accounts[str(mg.get("username", "admin"))] = str(mg["password"])
            if not self.auth.check(method, headers.get("authorization", ""), accounts):
                respond(401, b"unauthorized\n", "text/plain",
                        f"WWW-Authenticate: {self.auth.challenge()}\r\n")
            elif path == "/mlgwpservices.json":
                st = self.mlgw_store()
                if st is None:
                    respond(404, b"MLGW emulation is off\n", "text/plain")
                else:
                    log(f"MLGW config requested by {writer.get_extra_info('peername')[0]}")
                    mg = self.raw().get("mlgw") or {}
                    body_out = st.served(int(mg.get("api_port", 9000)), str(mg.get("serial", "")))
                    respond(200, json.dumps(body_out, ensure_ascii=False).encode(),
                            "application/json; charset=utf-8")
            elif path in ("/", "/index.html") and method == "GET":
                respond(200, self.index, "text/html; charset=utf-8")
            elif path.startswith("/api/"):
                code, obj = await asyncio.get_running_loop().run_in_executor(
                    None, self.api, method, path, body)
                respond(code, json.dumps(obj, ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")
            else:
                respond(404, b"not found\n", "text/plain")
        except Exception as e:
            log(f"{method} {path}: {type(e).__name__}: {e}")
            respond(500, json.dumps({"error": str(e)}).encode(), "application/json")
        try:
            await writer.drain()
        finally:
            writer.close()


async def serve(app: App) -> None:
    threading.Thread(target=app.key_watcher, name="keys", daemon=True).start()
    server = await asyncio.start_server(app.handle, port=app.port)
    log(f"web UI on port {app.port} (config: {app.config_path})")
    await server.serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()
    with open(args.config, "rb") as f:
        raw = tomllib.load(f)
    if not (raw.get("web") or {}).get("enabled", False):
        # Idle instead of exiting: the unit has Restart=always. Enable
        # [web] in the config, then: systemctl restart mdt-web
        log(f"[web] not enabled in {args.config} -- idle")
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.pause()
        return 0
    try:
        asyncio.run(serve(App(args.config, raw)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
