"""Device configuration of the MLGW emulation.

Kept in the MLGW's own format (what /mlgwpservices.json serves to Home
Assistant), so a real MLGW's export can be imported as-is:

    {"project": ..., "sn": ..., "port": 9000, "version": 2,
     "zones": [{"number": 1, "name": "Wohnzimmer",
                "products": [{"MLN": 1, "name": "BeoVision 10",
                              "mlAddress": 192,            # emulation only
                              "sources": [{"name": "TV", "selectID": 128,
                                           "statusID": 11, "destination": 0,
                                           "channels": [...], ...}]}]}]}

The one addition is `mlAddress` per product: the device's ML bus address,
which a real MLGW keeps internally. It is stripped from what HA gets.

The file is written only when the configuration is saved in the web UI,
atomically (temp file + rename in the same directory).

Serial number and project name: HA's integration needs both (the config
flow reads them, entity ids are built from the serial). An import from a
real MLGW brings its own; without one, a stable 8-digit serial is derived
from the Pi's /etc/machine-id (same value in every process, survives
updates) and stored with the next save, so it also survives a new SD card.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from typing import Optional

DEFAULT_PROJECT = "mdtv2"


def default_serial() -> str:
    """A stable 8-digit serial (like a real MLGW's) for this machine."""
    try:
        with open("/etc/machine-id") as f:
            seed = f.read().strip()
    except OSError:
        import socket
        seed = socket.gethostname()
    n = int(hashlib.sha256(f"mdtv2-mlgw:{seed}".encode()).hexdigest(), 16)
    return str(10_000_000 + n % 90_000_000)


class ConfigError(ValueError):
    pass


def validate(cfg: dict) -> None:
    """Raise ConfigError if `cfg` isn't usable."""
    if not isinstance(cfg.get("zones"), list):
        raise ConfigError("zones must be a list")
    mlns: set[int] = set()
    addrs: dict[int, int] = {}
    numbers: set[int] = set()
    for z in cfg["zones"]:
        n = z.get("number")
        if not isinstance(n, int) or n < 0 or n > 255:
            raise ConfigError(f"room {z.get('name')!r}: number must be 0-255")
        if n in numbers:
            raise ConfigError(f"room number {n} used twice")
        numbers.add(n)
        if not str(z.get("name", "")).strip():
            raise ConfigError(f"room {n}: name missing")
        for p in z.get("products", []):
            mln = p.get("MLN")
            if not isinstance(mln, int) or not 1 <= mln <= 255:
                raise ConfigError(f"device {p.get('name')!r}: MLN must be 1-255")
            if mln in mlns:
                raise ConfigError(f"MLN {mln} used twice")
            mlns.add(mln)
            a = p.get("mlAddress")
            if a is not None:
                if not isinstance(a, int) or not 0 <= a <= 0xFF:
                    raise ConfigError(f"MLN {mln}: ML address must be 0x00-0xFF")
                if a in addrs:
                    raise ConfigError(f"ML address 0x{a:02X} used by MLN "
                                      f"{addrs[a]} and {mln}")
                addrs[a] = mln
            for s in p.get("sources", []):
                if not str(s.get("name", "")).strip():
                    raise ConfigError(f"MLN {mln}: a source has no name")
                for k in ("selectID", "statusID", "destination"):
                    if not isinstance(s.get(k), int):
                        raise ConfigError(f"MLN {mln}, source {s.get('name')!r}: "
                                          f"{k} missing")


def normalize_source(s: dict) -> dict:
    """Fill the fields a real MLGW export carries, derived from the three
    the UI edits (selectID, statusID, destination)."""
    s.setdefault("format", "F0")
    s.setdefault("secondary", 0)
    s.setdefault("link", 0)
    s.setdefault("channels", [])
    s["selectCmds"] = [{"cmd": s["selectID"], "format": s.get("format", "F0"),
                        "unit": 0}]
    return s


class DeviceStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: dict = {"version": 2, "zones": []}

    def load(self) -> bool:
        """Load the file; False if it doesn't exist yet."""
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return False
        validate(data)
        self.data = data
        return True

    def seed(self, services: dict, addresses: dict[int, int]) -> None:
        """First start: take an MLGW export plus a MLN -> address map."""
        data = copy.deepcopy(services)
        for _z, p in self._products(data):
            if p.get("MLN") in addresses:
                p["mlAddress"] = addresses[p["MLN"]]
        validate(data)
        self.data = data

    def identity(self) -> tuple[str, str]:
        """(serial, project) -- the configured ones, or the defaults."""
        return (str(self.data.get("sn") or default_serial()),
                str(self.data.get("project") or DEFAULT_PROJECT))

    def save(self, new: dict) -> None:
        # Pin serial / project on the first save, so they don't change if
        # the machine-id does (new SD card, new Pi).
        new.setdefault("sn", self.data.get("sn") or default_serial())
        new.setdefault("project", self.data.get("project") or DEFAULT_PROJECT)
        new.setdefault("version", 2)
        for _z, p in self._products(new):
            for s in p.get("sources", []):
                normalize_source(s)
        validate(new)
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".devices-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        self.data = new

    def import_mlgw(self, services: dict) -> dict:
        """A real MLGW's export, keeping the ML addresses we already know
        for the same MLNs. Returns the merged config (not saved yet)."""
        known = self.addresses()
        merged = copy.deepcopy(services)
        for _z, p in self._products(merged):
            if p.get("MLN") in known and "mlAddress" not in p:
                p["mlAddress"] = known[p["MLN"]]
        return merged

    # ---- views -------------------------------------------------------------

    @staticmethod
    def _products(data: dict):
        for z in data.get("zones", []):
            for p in z.get("products", []):
                yield z, p

    def products(self):
        return self._products(self.data)

    def addresses(self) -> dict[int, int]:
        """MLN -> ML address."""
        return {p["MLN"]: p["mlAddress"] for _z, p in self.products()
                if isinstance(p.get("mlAddress"), int)}

    def by_address(self, addr: int) -> Optional[tuple[int, int, str]]:
        """ML address -> (MLN, room number, room name)."""
        for z, p in self.products():
            if p.get("mlAddress") == addr:
                return p["MLN"], z.get("number", 0), z.get("name", "")
        return None

    def served(self, port: int, serial: str) -> dict:
        """What HA gets: the MLGW format without our additions."""
        out = copy.deepcopy(self.data)
        for _z, p in self._products(out):
            p.pop("mlAddress", None)
        sn, project = self.identity()
        out["port"] = port
        out["sn"] = serial or sn
        out["project"] = project
        return out
