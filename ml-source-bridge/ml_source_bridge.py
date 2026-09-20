#!/usr/bin/env python3
"""ml-source-bridge -- pretend to be one ML device (AUDIO MASTER or
SOURCE CENTER) and bridge a Linux audio backend (the "provider") onto
the bus as one ML source byte.

Replaces the legacy split between ml-linkspeaker-standalone and
ml-netprovide. One process, one role at a time, configured statically
(role + source byte + display name + provider) via a TOML config file
or CLI flags.

The MCU firmware handles wire-level master/slave (PWR.DET pin) on its
own. Everything else is configuration. We don't try to auto-detect bus
topology -- empirically that's unreliable; topology fills in passively
from observed traffic instead.

Usage:

    # Run with config file (preferred for production)
    ./ml_source_bridge.py --config /etc/ml-source-bridge.toml

    # Run with CLI args (CLI overrides config values)
    ./ml_source_bridge.py --role sc --as 0xA1

    # Diagnostic dry-run -- log handler dispatch, never transmit
    ./ml_source_bridge.py --config /etc/ml-source-bridge.toml --dry-run

Config file (TOML, see config.toml.example):
    role           = "sc" | "am"
    source_byte    = 0xA1     (or 0x7A, 0x8D, 0x6F, ...)
    display_name   = "N.RADIO" (optional)
    provider       = "airplay" | "turntable"
    broadcast_clock= true
    auto_wake      = true
    redis_host     = "localhost"
    redis_port     = 6379
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import tomllib
from pathlib import Path
from typing import Any, Optional

# Make sibling packages (core, roles, providers) importable regardless of
# how this script was launched (direct ./ml_source_bridge.py vs systemd).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.bus import Bus, Periodic, detect_firmware_role, log
from core.dispatcher import Context, Dispatcher
from core import builders as B
from core import logging_setup
from core.light_handler import LightHandler
from core.telegram import (
    SRC_CD, SRC_N_MUSIC, SRC_N_RADIO, SRC_RADIO, TT_REQUEST,
)
from core.topology import Topology
from providers.airplay import AirPlayProvider
from providers.base import SourceProvider
from providers.multi import MultiSourceProvider
from providers.sendspin import SendspinProvider
from providers.turntable import TurntableProvider
from roles.audio_master import AudioMasterRole
from roles.source_center import SourceCenterRole
from roles.base import Role


# Default display names per source byte, mirroring what B&O firmware itself
# shows. Used when no display_name is configured.
_DEFAULT_DISPLAY = {
    SRC_RADIO:   "RADIO",
    SRC_N_MUSIC: "N.MUSIC",
    SRC_N_RADIO: "N.RADIO",
    SRC_CD:      "CD",
}


# Default config file search order (lowest precedence first; later wins).
# Always overridden by --config on the CLI.
_CONFIG_SEARCH = [
    "/etc/ml-source-bridge.toml",
    str(Path(__file__).parent / "config.toml"),
]


# ----------------------------------------------------------------------------

def load_config(explicit_path: Optional[str]) -> dict[str, Any]:
    """Load TOML config. If --config was passed, that file MUST exist.
    Otherwise fall through the default search list and use the first
    match. Returns an empty dict if nothing was found (CLI args / hard
    defaults take over)."""
    candidates = [explicit_path] if explicit_path else _CONFIG_SEARCH
    for path in candidates:
        if path and Path(path).is_file():
            log(f"[main] loading config: {path}")
            with open(path, "rb") as f:
                return tomllib.load(f)
        if explicit_path:
            raise SystemExit(f"config file not found: {explicit_path}")
    log("[main] no config file found; relying on CLI args + defaults")
    return {}


def _coalesce(*values, default=None):
    """First non-None value, else `default`. Used to layer CLI > config > default."""
    for v in values:
        if v is not None:
            return v
    return default


def _resolve_sources(cfg: dict, args) -> list[dict]:
    """Return a list of source dicts {source_byte, provider, display_name}.

    CLI `--as 0xNN` (with optional `--provider`/`--display`) defines a
    single-source ad-hoc list and OVERRIDES anything in the config file
    -- useful for one-off testing.

    Otherwise the config's `[[sources]]` array is used. Each entry must
    have at least `source_byte`; `provider` defaults to "airplay" and
    `display_name` is filled in from the per-source-byte default table
    if omitted.
    """
    if args.src is not None:
        return [{
            "source_byte":     args.src,
            "provider":        args.provider or "airplay",
            "provider_default": None,
            "display_name":    args.display,   # may be None -> default-table lookup
        }]
    raw = cfg.get("sources") or []
    if not isinstance(raw, list):
        raise SystemExit("config 'sources' must be an array (use [[sources]])")
    out = []
    for i, entry in enumerate(raw):
        if "source_byte" not in entry:
            raise SystemExit(
                f"config sources[{i}] is missing 'source_byte'")
        prov = entry.get("provider", "airplay")
        # provider can be either a plain string (single-provider source,
        # legacy shape) or a list of strings (multi-provider fan-out via
        # MultiSourceProvider). Anything else is a config error caught
        # later in make_provider().
        if isinstance(prov, list) and not prov:
            raise SystemExit(
                f"config sources[{i}] provider list is empty")
        out.append({
            "source_byte":      int(entry["source_byte"]),
            "provider":         prov,
            "provider_default": entry.get("provider_default"),
            "display_name":     entry.get("display_name"),
        })
    return out


# ----------------------------------------------------------------------------

def make_provider(name, source_byte: int, display_name: str,
                  *, cfg: Optional[dict] = None,
                  provider_default: Optional[str] = None,
                  redis_host: str = "localhost",
                  redis_port: int = 6379) -> SourceProvider:
    """Build a SourceProvider for one ML source byte.

    `name` is either a single provider name (string) or a list of names
    (multi-provider source, wrapped in MultiSourceProvider). For the
    multi case, `provider_default` selects which sub-provider gets a
    Play command when nothing is currently playing (defaults to the
    first entry in the list). Sub-provider display names come from a
    top-level `[provider_displays]` table in the config, keyed by
    provider name; falls back to the source-level display_name if the
    entry is missing.
    """
    if isinstance(name, list):
        displays_map = (cfg or {}).get("provider_displays") or {}
        subs: list[SourceProvider] = []
        sub_displays: list[str] = []
        for sub_name in name:
            if not isinstance(sub_name, str):
                raise SystemExit(
                    f"provider list entries must be strings, got {sub_name!r}")
            # Recurse: each sub is a plain single-provider build. Its
            # own display_name gets overwritten by the wrapper's dynamic
            # property, so pass the source-level fallback for logging.
            subs.append(make_provider(
                sub_name, source_byte, display_name,
                cfg=cfg, redis_host=redis_host, redis_port=redis_port))
            sub_displays.append(displays_map.get(sub_name, display_name))
        # Default sub: named in config, or first entry.
        if provider_default is None:
            default_idx = 0
        else:
            try:
                default_idx = name.index(provider_default)
            except ValueError:
                raise SystemExit(
                    f"provider_default={provider_default!r} not in "
                    f"provider list {name!r}")
        return MultiSourceProvider(
            source_byte=source_byte,
            fallback_display=display_name,
            subs=subs,
            sub_displays=sub_displays,
            default_idx=default_idx,
        )

    # Single-provider (legacy) shape.
    if name == "airplay":
        return AirPlayProvider(source_byte=source_byte,
                               display_name=display_name)
    if name == "sendspin":
        return SendspinProvider(source_byte=source_byte,
                                display_name=display_name)
    if name == "turntable":
        # Provider-specific settings live in their own [turntable] table
        # (ALSA devices, RIAA, ADC gain, DL'80 opcode overrides).
        return TurntableProvider(source_byte=source_byte,
                                 display_name=display_name,
                                 cfg=(cfg or {}).get("turntable") or {},
                                 redis_host=redis_host,
                                 redis_port=redis_port)
    raise SystemExit(f"unknown provider: {name!r}")


def parse_wake_target(value: object) -> object:
    """Normalise the `wake_target` setting.

    Accepts "vm" | "am" | "off" (case-insensitive) or an ML address for
    setups where the wake should go somewhere else entirely -- given as
    an int (0x06) or a string ("0x06" / "6"). Returns "vm"/"am"/"off" or
    an int address.
    """
    if value is None:
        return "vm"
    if isinstance(value, bool):                      # bool is an int; reject
        raise SystemExit("config wake_target must be vm/am/off or an address")
    if isinstance(value, int):
        return value & 0xFF
    text = str(value).strip().lower()
    if text in ("vm", "am", "off"):
        return text
    try:
        return int(text, 0) & 0xFF
    except ValueError:
        raise SystemExit(
            f"config wake_target {value!r} is not 'vm', 'am', 'off' "
            f"or an ML address like 0x06")


def make_role(name: str) -> Role:
    if name == "am":
        return AudioMasterRole()
    if name == "sc":
        return SourceCenterRole()
    raise SystemExit(f"unknown role: {name!r}  (must be 'am' or 'sc')")


# ---- dry-run wrapper around Bus --------------------------------------------

class _DryRunBus:
    """Wraps a real Bus but swallows send() calls; listen still works."""
    def __init__(self, real: Bus) -> None:
        self._real = real
    def send(self, telegram_bytes: bytes) -> None:
        log(f"[dry] would send: {telegram_bytes.hex()}")
    def listen_ml(self):
        return self._real.listen_ml()
    def close(self) -> None:
        self._real.close()
    @property
    def stop(self):
        return self._real.stop
    @property
    def r(self):
        return self._real.r


# ---- per-provider metadata pump (SC only) ----------------------------------

def _metadata_pump_factory(role: Role, ctx: Context,
                           provider: SourceProvider, last: dict):
    """Poll one provider's metadata; on change, fire the role's
    'metadata' event with that provider attached."""
    def pump() -> None:
        md = provider.metadata()
        if md is None:
            return
        sig = (md.title, md.album, md.artist)
        if sig == last.get("sig"):
            return
        last["sig"] = sig
        role.on_provider_event(ctx, "metadata", provider)
    return pump


# ---- per-provider stream-state watcher -------------------------------------

# Number of consecutive "stopped" polls required before we believe the
# stream has actually stopped. With a 1-second poll interval, 5 polls =
# ~5 seconds of grace. This protects against shairport-sync briefly
# dropping PlaybackStatus from "Playing" to "Paused" while the bus is
# being woken (audio buffers without flowing, MPRIS thinks idle, but
# we're mid-wake and don't want to abort with a RELEASE that kills the
# in-progress activation).
_STOP_DEBOUNCE_POLLS = 5


def _stream_watcher_factory(role: Role, ctx: Context,
                            provider: SourceProvider, state: dict):
    """Detect provider.is_playing() edges and fire the role's
    stream_started / stream_stopped events.

    Edge logic:
      false -> true : fire stream_started immediately
      true  -> false: require _STOP_DEBOUNCE_POLLS consecutive False
                      polls before firing stream_stopped. Avoids
                      false-stops during bus wake-up.
    """
    def watch() -> None:
        try:
            cur = bool(provider.is_playing())
        except Exception as e:
            log(f"[watch] {provider.display_name!r} is_playing() raised: {e}",
                err=True)
            return
        prev = state.get("playing", False)

        if cur:
            # Currently playing -- reset the stop counter regardless
            # of prior state.
            state["stop_streak"] = 0
            if not prev:
                state["playing"] = True
                log(f"[watch] {provider.display_name!r} "
                    f"(0x{provider.source_byte:02x}) "
                    f"stream STARTED (prev=False, now=True)")
                role.on_provider_event(ctx, "stream_started", provider)
            return

        # Currently NOT playing.
        if not prev:
            # Was already stopped -- stay silent.
            state["stop_streak"] = 0
            return

        # Was playing, now poll says not. Count consecutive stops.
        streak = state.get("stop_streak", 0) + 1
        state["stop_streak"] = streak
        if streak < _STOP_DEBOUNCE_POLLS:
            log(f"[watch] {provider.display_name!r} "
                f"is_playing()=False ({streak}/{_STOP_DEBOUNCE_POLLS}) "
                f"-- waiting for confirmation before declaring stop")
            return

        # Confirmed stopped after debounce.
        state["playing"] = False
        state["stop_streak"] = 0
        log(f"[watch] {provider.display_name!r} "
            f"(0x{provider.source_byte:02x}) "
            f"stream STOPPED (debounced after {streak} polls)")
        role.on_provider_event(ctx, "stream_stopped", provider)
    return watch


# ---- main loop --------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--config", default=None,
                    help="path to TOML config file (default: search "
                         "/etc/ml-source-bridge.toml then ./config.toml)")
    # Each of these, if given, overrides the config file value.
    ap.add_argument("--role", choices=["am", "sc"],
                    help="ML bus role (overrides config)")
    ap.add_argument("--as", dest="src",
                    type=lambda s: int(s, 0),
                    help="ML source byte, e.g. 0xA1 (overrides config)")
    ap.add_argument("--display", default=None,
                    help="display name shown on B&O panels (overrides config)")
    ap.add_argument("--provider", default=None,
                    help="audio backend (overrides config)")
    ap.add_argument("--redis-host", default=None)
    ap.add_argument("--redis-port", type=int, default=None)
    ap.add_argument("--no-clock", action="store_true",
                    help="disable periodic CLOCK broadcast (overrides config)")
    ap.add_argument("--no-wake", action="store_true",
                    help="disable auto-wake on stream start (overrides config)")
    ap.add_argument("--dry-run", action="store_true",
                    help="log dispatch but never transmit")
    ap.add_argument("--log-file", default=None,
                    help="path to log file (default: /tmp/ml-source-bridge.log; "
                         "pass an empty string to disable file logging)")
    ap.add_argument("--debug", action="store_true",
                    help="set log level to DEBUG (very verbose)")
    args = ap.parse_args()

    # ---- merge config + CLI ------------------------------------------------
    cfg = load_config(args.config)

    # Configure logging early so even early-startup messages land in the
    # log file. CLI > config > default; empty string disables file logging.
    log_file = args.log_file
    if log_file is None:
        log_file = cfg.get("log_file", logging_setup.DEFAULT_LOG_FILE)
    if log_file == "":
        log_file = None
    import logging
    logging_setup.setup(
        log_file=log_file,
        level=logging.DEBUG if args.debug else logging.INFO,
    )
    if log_file:
        log(f"[main] logging to {log_file} (and stderr)")

    role_name   = _coalesce(args.role,         cfg.get("role"),         default=None)
    redis_host  = _coalesce(args.redis_host,   cfg.get("redis_host"),   default="localhost")
    redis_port  = _coalesce(args.redis_port,   cfg.get("redis_port"),   default=6379)
    do_clock = not args.no_clock and cfg.get("broadcast_clock", True)
    do_wake  = not args.no_wake  and cfg.get("auto_wake",       True)
    wake_target = parse_wake_target(cfg.get("wake_target"))

    # Build the source list. Two forms supported:
    #   - config has [[sources]] array: each entry is { source_byte, provider, display_name? }
    #   - CLI has --as: makes a single-source ad-hoc list (overrides config sources)
    sources_list = _resolve_sources(cfg, args)

    if not sources_list:
        raise SystemExit(
            "no sources configured. Add [[sources]] entries to the config "
            "file or pass --as 0xNN on the CLI.")

    if role_name is None:
        # No explicit role -- use the firmware pin as a last-resort default.
        log("[main] no role configured; falling back to firmware pin")
        bus_temp = Bus(host=redis_host, port=redis_port,
                       stop=threading.Event())
        try:
            fw = detect_firmware_role(bus_temp, timeout=1.0)
        finally:
            bus_temp.close()
        role_name = fw or "sc"
        log(f"[main] firmware pin -> role={role_name!r} "
            f"(set 'role' in config for production)")

    # ---- signals + bus ----------------------------------------------------
    stop = threading.Event()
    signal.signal(signal.SIGINT,  lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    bus_real = Bus(host=redis_host, port=redis_port, stop=stop)
    bus = _DryRunBus(bus_real) if args.dry_run else bus_real

    role = make_role(role_name)
    setattr(role, "wake_target", wake_target)

    # Build providers dict: source byte -> SourceProvider.
    # A source with a list-valued `provider` is transparently wrapped in
    # a MultiSourceProvider by make_provider(); from here it looks like
    # any other single provider.
    providers: dict[int, SourceProvider] = {}
    for entry in sources_list:
        src        = entry["source_byte"]
        pname      = entry.get("provider", "airplay")
        pdefault   = entry.get("provider_default")
        display    = entry.get("display_name") or _DEFAULT_DISPLAY.get(
            src, f"0x{src:02x}")
        if src in providers:
            raise SystemExit(
                f"duplicate source_byte 0x{src:02x} in config -- "
                f"each source byte may have only one entry")
        providers[src] = make_provider(
            pname, src, display, cfg=cfg,
            provider_default=pdefault,
            redis_host=redis_host, redis_port=redis_port)

    log(f"[main] role={role.name} addr=0x{role.own_address:02x}  "
        f"redis={redis_host}:{redis_port}  "
        f"clock={'on' if do_clock else 'off'}  "
        f"wake={'on' if do_wake else 'off'}"
        + (f"->{wake_target if isinstance(wake_target, str) else hex(wake_target)}"
           if do_wake else "") + "  "
        f"dry_run={args.dry_run}")
    for src, prov in providers.items():
        log(f"[main]   source 0x{src:02x}  provider={prov.__class__.__name__}"
            f"  display={prov.display_name!r}")

    # Sanity check against firmware pin (informational; warns on disagreement).
    if not args.dry_run:
        fw = detect_firmware_role(bus_real, timeout=1.0)
        if fw is not None and fw != role.name:
            log(f"[main] NOTE: firmware pin reports role={fw!r} but "
                f"config says role={role.name!r}. This may be intentional "
                f"(e.g. you're forcing AM in a VM-only setup) but double-check.")

    # ---- dispatcher + topology --------------------------------------------
    topology = Topology()
    dispatcher = Dispatcher()
    role.install_handlers(dispatcher)
    ctx = Context(bus=bus, role=role, providers=providers, topology=topology)

    # ---- optional LIGHT-key home-automation handler -----------------------
    light_handler: Optional[LightHandler] = None
    light_cfg = cfg.get("light_handler") or {}
    if light_cfg.get("enabled", False):
        cmd_map = light_cfg.get("commands") or {}
        if not cmd_map:
            log("[main] [light_handler] enabled=true but no commands "
                "configured -- nothing will trigger", err=True)
        else:
            light_handler = LightHandler(
                commands=cmd_map,
                timeout_s=float(light_cfg.get("timeout_s", 20.0)),
            )
            # Report what actually got parsed (after name resolution +
            # dedup) so the user can spot typos like "stepup" vs
            # "step_up" or missing aliases.
            if light_handler.commands:
                bindings = ", ".join(
                    f"0x{code:02x}" for code in sorted(light_handler.commands)
                )
                log(f"[main] light_handler armed; bound keys: {bindings}")
            else:
                log("[main] light_handler enabled but no usable bindings "
                    "-- nothing will fire (see earlier warnings)", err=True)

    # ---- background threads -----------------------------------------------
    threads: list[threading.Thread] = []

    if do_clock:
        threads.append(Periodic(
            fn=lambda: bus.send(B.clock(frm=role.own_address)),
            interval_s=1800.0, initial_delay_s=10.0,
            stop=stop, name="clock"))

    # One stream-watcher + (SC only) one metadata pump per provider.
    is_sc = isinstance(role, SourceCenterRole)
    for src, prov in providers.items():
        if is_sc:
            threads.append(Periodic(
                fn=_metadata_pump_factory(role, ctx, prov, {}),
                interval_s=1.0, initial_delay_s=5.0,
                stop=stop, name=f"metadata-0x{src:02x}"))
        if do_wake:
            threads.append(Periodic(
                fn=_stream_watcher_factory(role, ctx, prov, {"playing": False}),
                interval_s=1.0, initial_delay_s=2.0,
                stop=stop, name=f"watch-0x{src:02x}"))

    for th in threads:
        th.start()

    for prov in providers.values():
        prov.start()

    # Role boot announce -- emit any "I exist on the bus" telegrams the
    # real B&O device with this role would send at power-up (see captured
    # ml-startup.txt for the SC sequence). Without this, other devices
    # don't have us in their device list and our later telegrams may be
    # treated as malformed.
    if not args.dry_run:
        try:
            role.on_startup(ctx)
        except Exception as e:
            log(f"[main] role.on_startup() raised: {e}", err=True)

    # ---- main listen loop -------------------------------------------------
    try:
        for t in bus.listen_ml():
            # Passive topology tracking: every FROM we observe goes into
            # `topology` so wake logic can pick the right wake target.
            topology.saw(t.from_addr)
            # Passive role-level observation -- runs for every telegram
            # regardless of address, so cross-cutting state like the
            # lock-manager position can be tracked even when telegrams
            # are addressed to other devices.
            try:
                role.passive_observe(t, ctx)
            except Exception as e:
                log(f"[main] role.passive_observe raised: {e}", err=True)
            # LIGHT-key handler also runs for every telegram (telegrams
            # are addressed to MLGW=0xF0, not to us). No-op when no
            # [light_handler] section was configured.
            if light_handler is not None:
                try:
                    light_handler.observe(t)
                except Exception as e:
                    log(f"[main] light_handler.observe raised: {e}",
                        err=True)
            if not role.matches_us(t):
                continue
            handled = dispatcher.dispatch(t, ctx)
            if not handled and t.telegram_type == TT_REQUEST:
                # Only flag unhandled REQUESTs -- those expect us to answer.
                # CMD/RSP/INFO traffic from VM about its own state etc. is
                # silently dropped.
                log(f"[main] unhandled REQUEST pl_type=0x{t.payload_type:02x} "
                    f"src_dest=0x{t.src_dest:02x} from=0x{t.from_addr:02x}")
    finally:
        log("[main] shutting down")
        if not args.dry_run:
            try:
                bus.send(B.release(frm=role.own_address))
            except Exception:
                pass
        for prov in providers.values():
            try:
                prov.stop()
            except Exception as e:
                log(f"[main] error stopping {prov.display_name!r}: {e}",
                    err=True)
        bus.close()
        for th in threads:
            th.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
