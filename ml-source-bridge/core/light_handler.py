"""LIGHT-key home-automation hook.

Background
----------
The original B&O MasterLink Gateway (MLGW, address 0xF0) is a small
appliance that lets Beo4 remotes drive a home-automation system. The
"LIGHT" key on the remote opens what the system calls a "light
channel": after the user presses LIGHT, every subsequent keypress is
forwarded by the VM/AM to MLGW for ~20 s. After 20 s of inactivity
the master synthesises a "Light Timeout" key (0x58) which closes the
channel.

That means EVERY Beo4 key can be a light command, not just digits:
LIGHT then Step-Up could "brighten", LIGHT then Play could "toggle",
LIGHT then 1 could "evening scene". One LIGHT press, many actions
back-to-back, all while the channel is open.

We emulate the MLGW-side behaviour: snoop the bus for virtual_beo4
telegrams addressed to MLGW, and run a configured shell command for
each (key) seen while the channel is open. The original MLGW was a
hardware box; here it's a few lines of Python and whatever scripts
the user wants to wire in (Home Assistant calls, MQTT, GPIO, ...).

Captured wire behaviour (VM forwarding, 2026-05-19):

    LIGHT pressed:
        TO=0xF0 (MLGW)  FROM=0xC0 (VM)  PT=0x20 (VIRTUAL_BEO4)
            payload: 02 00 1b f8 0a    cmd=0x9B (Light)  DEST=0x1B (MLGW)

    any key within ~20 s:
        TO=0xF0  FROM=0xC0  PT=0x20
            payload: 02 00 1b f8 0a    cmd=0xXX (any Beo4 code)

    no follow-up within 20 s:
        TO=0xF0  FROM=0xC0  PT=0x20
            payload: 02 00 1b f8 0a    cmd=0x58 (Light Timeout)

We only care about (PT_VIRTUAL_BEO4, TO=ADDR_MLGW). The 8-bit Beo4
command sits at telegram byte 14 (the post-payload byte; pl_len
declares 5 but the receiver reads a 6th byte for the key). This
off-by-one matches what the rest of the codebase already assumes for
virtual_beo4 frames.
"""
from __future__ import annotations

import subprocess
import threading
from typing import Optional

from core.bus import log
from core.telegram import (
    ADDR_MLGW,
    KEY_LIGHT, KEY_LIGHT_TIMEOUT,
    PT_VIRTUAL_BEO4,
    Telegram,
)


# Hard cap on each spawned shell command. Stops a runaway curl /
# hung script from accumulating zombie threads.
_COMMAND_TIMEOUT_S = 30.0


# Friendly-name aliases for the Beo4 key codes most likely to be used
# in [light_handler.commands]. Users can write either a name (any of
# these), a hex literal ("0x1e"), or a decimal int (30) in TOML; all
# three forms parse to the same key code. Names are matched case-
# insensitively after normalising spaces and hyphens to underscores.
#
# Source for the codes: ml-debug/const.py / B&O Beo4 protocol notes.
# Not every Beo4 key is here -- only the ones a sensible light-channel
# binding would use. For anything missing, just use the hex form.
BEO4_KEY_NAMES: dict[str, int] = {
    # Digits (both short and long forms accepted)
    "0": 0x00, "digit_0": 0x00,
    "1": 0x01, "digit_1": 0x01,
    "2": 0x02, "digit_2": 0x02,
    "3": 0x03, "digit_3": 0x03,
    "4": 0x04, "digit_4": 0x04,
    "5": 0x05, "digit_5": 0x05,
    "6": 0x06, "digit_6": 0x06,
    "7": 0x07, "digit_7": 0x07,
    "8": 0x08, "digit_8": 0x08,
    "9": 0x09, "digit_9": 0x09,
    # Transport / navigation
    "step_up":      0x1E,
    "step_down":    0x1F,
    "rewind":       0x32,
    "return":       0x33,
    "wind":         0x34,
    "play":         0x35,    "go": 0x35,    "go_play": 0x35,
    "stop":         0x36,
    "record":       0x37,
    # Sound
    "mute":         0x0D,
    "p_mute":       0x1C,
    "volume_up":    0x60,
    "volume_down":  0x64,
    # Colour keys (common on Beo4 / Beo5)
    "yellow":       0xD4,
    "green":        0xD5,
    "blue":         0xD8,
    "red":          0xD9,
    # Cursor / menu
    "cursor_up":    0xCA,
    "cursor_down":  0xCB,
    "cursor_left":  0xCC,
    "cursor_right": 0xCD,
    "select":       0x13,
    "back":         0x14,
    "exit":         0x7F,
    "menu":         0x5C,
    "info":         0x43,
    "guide":        0x40,
    # Miscellaneous
    "standby":      0x0C,
    "clear":        0x0A,
    "store":        0x0B,
    "reset":        0x0E,
    "goto":         0x20,
    "format":       0x2A,
    "sound":        0x46,
}


def _parse_beo4_key(key: object) -> Optional[int]:
    """Normalise a TOML key from [light_handler.commands] into a Beo4
    code byte. Accepts: int (kept as-is), hex string ("0x1e"), decimal
    string ("30"), or a friendly name from BEO4_KEY_NAMES (case-
    insensitive, with spaces or hyphens). Returns None if it can't be
    interpreted (caller logs + ignores)."""
    if isinstance(key, int):
        return key & 0xFF
    s = str(key).strip()
    if not s:
        return None
    # Try integer first (handles "0x1e", "30", "0o36", etc.)
    try:
        return int(s, 0) & 0xFF
    except ValueError:
        pass
    # Fall back to the name table, case-folded and with space/hyphen
    # normalised to underscore.
    norm = s.lower().replace("-", "_").replace(" ", "_")
    return BEO4_KEY_NAMES.get(norm)


class LightHandler:
    """Observer-style: feed every received Telegram via `observe()`.

    State machine:
        IDLE       -- waiting for LIGHT
        CHANNEL    -- LIGHT pressed; every subsequent Beo4 key looks
                      up its configured shell command and (if present)
                      runs it. The channel stays open until the bus
                      sends LIGHT_TIMEOUT (or our safety timer fires).

    Transitions:
        IDLE     -> CHANNEL    on LIGHT
        CHANNEL  -> CHANNEL    on any other key   (runs configured
                                                   command if any;
                                                   safety timer reset)
        CHANNEL  -> IDLE       on LIGHT_TIMEOUT from the bus, or our
                                 own safety timer expiring
    """

    def __init__(self, commands: dict[object, str], *,
                 timeout_s: float = 20.0) -> None:
        # Resolve every TOML key into a Beo4 code byte. Names like
        # "play", "step_up", "red", "digit_1" all work; so do raw hex
        # ("0x1e") and decimals (30). Unknown keys are reported (so a
        # typo doesn't fail silently) and dropped.
        self.commands: dict[int, str] = {}
        for k, v in commands.items():
            code = _parse_beo4_key(k)
            if code is None:
                log(f"[light] config: unknown Beo4 key {k!r}, ignoring "
                    f"(use a hex code like '0x1e' or a name like "
                    f"'step_up' / 'play' / 'red' / 'digit_1')", err=True)
                continue
            if code in self.commands:
                log(f"[light] config: duplicate binding for Beo4 "
                    f"0x{code:02x} via key {k!r}; later one wins")
            self.commands[code] = str(v)
        self.timeout_s = float(timeout_s)
        self._state = "idle"
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    # ---- bus observer ---------------------------------------------------

    def observe(self, t: Telegram) -> None:
        """Called for every Telegram on the bus, regardless of role/
        target. Cheap when the telegram doesn't match our filter."""
        if t.payload_type != PT_VIRTUAL_BEO4:
            return
        if t.to_addr != ADDR_MLGW:
            return
        # The Beo4 command byte sits AFTER the declared pl_len payload
        # (B&O off-by-one quirk used by every other virtual_beo4 builder
        # / parser in this codebase).
        if len(t.raw) < 15:
            return
        self._on_key(t.raw[14])

    # ---- state machine --------------------------------------------------

    def _on_key(self, cmd: int) -> None:
        with self._lock:
            if cmd == KEY_LIGHT:
                log(f"[light] LIGHT channel opened (timeout "
                    f"{self.timeout_s:.0f}s; press any key now)")
                self._arm()
                return

            if cmd == KEY_LIGHT_TIMEOUT:
                if self._state == "channel":
                    log("[light] bus reported Light Timeout -> channel closed")
                    self._disarm()
                return

            if self._state != "channel":
                # Not in the LIGHT channel and not a LIGHT/TIMEOUT key.
                # Telegrams to MLGW happen all the time during normal
                # remote use (every Beo4 keypress is forwarded); we
                # silently ignore them when no channel is open.
                return

            # In CHANNEL: this is a candidate light command. Reset the
            # safety timer (the bus likely resets its own ~20s window
            # on every keypress) and run the configured shell command
            # if one is bound to this key.
            self._reset_timer()
            command = self.commands.get(cmd)
            if command is None:
                log(f"[light] channel: key 0x{cmd:02x} pressed -- "
                    f"no binding configured")
                return
            log(f"[light] channel: key 0x{cmd:02x} -> {command}")
            self._spawn(cmd, command)

    # Safety margin on top of the user-configured timeout. The bus
    # emits its own LIGHT_TIMEOUT key at ~20 s of inactivity -- we'd
    # rather honour that authoritative signal than fire our local
    # fallback first. Local fires only if the bus telegram is lost.
    _SAFETY_MARGIN_S = 5.0

    def _arm(self) -> None:
        self._state = "channel"
        self._reset_timer()

    def _reset_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(
            self.timeout_s + self._SAFETY_MARGIN_S,
            self._on_local_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _disarm(self) -> None:
        self._state = "idle"
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_local_timeout(self) -> None:
        with self._lock:
            if self._state == "channel":
                log("[light] safety timer fired (no bus Light Timeout "
                    "received) -> channel closed")
                self._state = "idle"
                self._timer = None

    # ---- scene runner ---------------------------------------------------

    def _spawn(self, digit: int, command: str) -> None:
        """Fire-and-forget shell command in a background thread.
        Output is discarded; stderr is captured and logged on failure;
        a 30 s wall-clock timeout prevents runaway scripts from
        accumulating threads."""
        def run() -> None:
            try:
                proc = subprocess.run(
                    command, shell=True, check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=_COMMAND_TIMEOUT_S,
                )
                if proc.returncode != 0:
                    err = proc.stderr.decode("utf-8", errors="replace").strip()
                    log(f"[light] scene {digit} exited {proc.returncode}: "
                        f"{err[:200]}", err=True)
            except subprocess.TimeoutExpired:
                log(f"[light] scene {digit} killed after "
                    f"{_COMMAND_TIMEOUT_S:.0f}s timeout", err=True)
            except Exception as e:
                log(f"[light] scene {digit} crashed: {e}", err=True)
        threading.Thread(target=run, name=f"light-scene-{digit}",
                         daemon=True).start()
