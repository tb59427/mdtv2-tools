"""DL'80 turntable provider -- a Beogram as an ML source.

CONTROL. ML transport events (delivered by the role layer) become
single-byte DL'80 opcodes published on `link:dl80:transmit`; the broker
does the wire-level repeat. Defaults are the opcodes verified on a
Beogram 5500 (see dl-docs/dl80-beogram/beogram-5500.md):

    play 0xA9 BG.Play         next 0x95 BG.ADV->
    stop 0xCB Sys.Standby     prev 0xF3 BG.<-Ret

`stop` is deliberately Sys.Standby, not BG.Standby (0x9A) -- the latter
has no observable effect on a 5500. Note Sys.Standby is bus-wide: other
DL'80 devices on the same wire go to standby too. All four are
overridable in config for other Beogram models.

AUDIO. A turntable is analogue, so unlike the AirPlay provider (where
shairport-sync feeds the DAC by itself) we have to move samples
ourselves: ADC capture -> optional software RIAA -> DAC playback. Both
directions are the same sound card on the HAT, so it's one clock domain
and there's no resampling drift. The ADC's input mux and PGA gain are set
over I2C first, mirroring dl-scripts/dl80-turntable/capture-phono-*.sh.

STATE. `is_playing()` is primarily what we last commanded. On top of that
it follows the Beogram's own DL'80 status echoes (0xC3 Playing / 0xCE
Stopped / 0xC5 Standby / 0xC8 NoMedia) snooped from `link:dl80:receive`,
so on a deck that reports those, pressing PLAY on the turntable itself
flips is_playing and the role's auto-wake switches the ML system to this
source -- the same edge an AirPlay session start produces.

VERIFIED 2026-08-20 against the Beogram 5500 on the mdtv2 bench, and two
findings are worth knowing:

  * The ADC input mux really matters. The PCM1862 came up on VIN1 while
    the phono input is VIN4, so without the mux write there is simply no
    signal -- exactly the "it plays but I hear nothing" trap. With it,
    a playing record measured RMS ~-35 dBFS / peak ~-15 dBFS. PGA gain
    also checked out: +12 dB requested measured +12.9 dB.

  * This deck does NOT emit the documented status codes. Commanding play
    / next / standby produced only 0xFC (Sys.Release) and 0xF1, never
    0xC3 / 0xCE / 0xC5. Those two are not state (0xFC came back for both
    play and next), so we deliberately do not map them. Consequence on
    such a deck: is_playing follows our commands only, so (a) starting
    the record by hand does not wake ML, and (b) when a side ends we keep
    reporting playing until the ML side releases the source. Decks that
    do report status get both behaviours for free.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Optional

import redis

from core.bus import log
from providers.base import Metadata, SourceProvider

DL80_TX = "link:dl80:transmit"
DL80_RX = "link:dl80:receive"

# Verified Beogram 5500 opcodes (config-overridable).
OP_PLAY = 0xA9
OP_STOP = 0xCB
OP_NEXT = 0x95
OP_PREV = 0xF3

# Status echoes the Beogram sends back. Anything in _PLAYING_ST means the
# platter is turning; _STOPPED_ST means it isn't.
# 0xFC / 0xF1 are what a Beogram 5500 actually emits -- established
# 2026-08-20 from six consistent data points: pressing the deck's own PLAY
# button gave 0xFC and its STANDBY button gave 0xF1 (twice each), and the
# same two codes came back when we commanded play / standby over DL'80.
# The spec names for them ("Sys.Release", "BG.Status.RecInput?") do not
# describe that behaviour, so trust the observation, not the label. Override
# per model with status_playing / status_stopped in config.
_PLAYING_ST = {
    0xC3,   # BG.Status.Playing
    0xC4,   # BG.Status.StepAdv->
    0xD2,   # BG.Status.FF->>
    0xFC,   # observed: Beogram 5500 says this when it starts
}
_STOPPED_ST = {
    0xC5,   # BG.Status.Standby
    0xC8,   # BG.Status.NoMedia
    0xCE,   # BG.Status.Stopped
    0xF1,   # observed: Beogram 5500 says this when it stops
}

# The AM/VM fires several RELEASE/STANDBY telegrams during teardown; don't
# re-send the same opcode for each one.
_TRANSPORT_DEBOUNCE_S = 0.4

# After we command a stop, ignore "playing" echoes for a moment: the
# turntable can still be coasting, and letting that flip is_playing back
# to True would have the role wake the bus again immediately.
_ECHO_GRACE_S = 3.0

# Ignore a pause that lands right on top of a play. Selecting our source
# produces a DIST_REQUEST (-> play), but the AM also re-sends RELEASE for
# our source as it tears the PREVIOUS session down, and that arrives a
# fraction of a second AFTER the grant -- observed 154 ms. Without this
# window the turntable spins up and is immediately parked again. Nobody
# selects a source and leaves it inside two seconds, so swallowing a pause
# this early costs nothing.
_PLAY_GRACE_S = 2.0

# PCM1862 ADC registers (same ones the capture scripts poke).
_REG_PGA_L    = 0x01
_REG_PGA_R    = 0x02
_REG_ADC1L_IN = 0x06
_REG_ADC1R_IN = 0x07
# 0x48 = VIN4, single-ended -- where the HAT wires the phono input.
_ADC_IN_DEFAULT = 0x48
# PGA is 0.5 dB per step, two's complement, -12 dB .. +40 dB.
_PGA_MIN_DB, _PGA_MAX_DB = -12.0, 40.0


def _pga_reg(db: float) -> int:
    """dB -> PCM1862 PGA register byte (0.5 dB steps, two's complement)."""
    db = max(_PGA_MIN_DB, min(_PGA_MAX_DB, float(db)))
    return int(round(db * 2)) & 0xFF


class TurntableProvider(SourceProvider):
    def __init__(self, *, source_byte: int, display_name: str,
                 cfg: Optional[dict] = None,
                 redis_host: str = "localhost",
                 redis_port: int = 6379) -> None:
        cfg = cfg or {}
        self.source_byte = source_byte
        self.display_name = display_name

        ops = cfg.get("opcodes") or {}
        self._op_play = int(ops.get("play", OP_PLAY))
        self._op_stop = int(ops.get("stop", OP_STOP))
        self._op_next = int(ops.get("next", OP_NEXT))
        self._op_prev = int(ops.get("prev", OP_PREV))

        # ---- audio loopback -------------------------------------------------
        self._loopback = bool(cfg.get("loopback", True))
        self._cap_dev = str(cfg.get("capture_device", "hw:sndrpihifiberry,0"))
        self._play_dev = str(cfg.get("playback_device", "hw:sndrpihifiberry,0"))
        self._rate = int(cfg.get("sample_rate", 48000))
        self._channels = int(cfg.get("channels", 2))
        self._format = str(cfg.get("format", "S32_LE"))
        self._period = int(cfg.get("period_size", 0))     # 0 = let ALSA pick

        # ---- software RIAA --------------------------------------------------
        self._riaa = bool(cfg.get("riaa", False))
        self._riaa_hp = float(cfg.get("riaa_highpass_hz", 30.0))
        self._riaa_gain = float(cfg.get("riaa_gain_db", 0.0))

        # ---- ADC front end --------------------------------------------------
        self._adc_setup = bool(cfg.get("adc_setup", True))
        self._pga_db = float(cfg.get("pga_db", 0.0))
        self._i2c_bus = int(cfg.get("i2c_bus", 1))
        self._i2c_addr = str(cfg.get("i2c_addr", "0x4a"))
        self._adc_input = int(cfg.get("adc_input_reg", _ADC_IN_DEFAULT))
        # i2cset lives in /usr/sbin, which isn't on every PATH (notably a
        # non-login shell). Resolve it once instead of trusting PATH.
        self._i2cset = str(cfg.get("i2cset_path") or shutil.which("i2cset")
                           or "/usr/sbin/i2cset")

        self._r = redis.StrictRedis(host=redis_host, port=redis_port, db=0,
                                    socket_keepalive=True)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._playing = False
        self._last_sent: dict[int, float] = {}
        self._grace_until = 0.0
        self._play_at = 0.0
        # Shown on the B&O display as the "track" text. "" disables.
        self._title = str(cfg.get("metadata_title", "PHONO"))
        # Deck-specific status codes (see _PLAYING_ST / _STOPPED_ST above).
        self._st_play = set(cfg.get("status_playing") or _PLAYING_ST)
        self._st_stop = set(cfg.get("status_stopped") or _STOPPED_ST)
        # Pressing STANDBY on the deck should switch the ML system off, not
        # just drop the source. The role reads this flag on stream_stopped;
        # providers don't send telegrams themselves.
        self.standby_ml_on_stop = bool(cfg.get("standby_ml_on_stop", True))
        self._snoop_stop = threading.Event()
        self._snoop_thread: Optional[threading.Thread] = None

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin snooping DL'80 status. Does NOT spin the platter -- that
        happens on play(), when the bus grants us the source."""
        if self._snoop_thread is None:
            self._snoop_thread = threading.Thread(
                target=self._snoop, name="tt-dl80-snoop", daemon=True)
            self._snoop_thread.start()
        log(f"[tt] ready on 0x{self.source_byte:02x} "
            f"({self.display_name!r}); loopback="
            f"{'on' if self._loopback else 'off'} "
            f"riaa={'on' if self._riaa else 'off'} "
            f"pga={self._pga_db:+g}dB")

    def stop(self) -> None:
        self.pause()
        self._snoop_stop.set()

    # ---- transport ---------------------------------------------------------

    def play(self) -> None:
        self._play_at = time.time()
        if self._adc_setup:
            self._configure_adc(self._pga_db)
        self._start_audio()
        if self._playing:
            # Already spinning -- someone pressed PLAY on the deck and the
            # bus is only now catching up. Re-sending BG.Play would re-cue
            # the arm to track 1, so just make sure the audio path is up.
            log("[tt] already playing (started on the deck) -- "
                "not re-sending BG.Play")
            return
        if self._send(self._op_play, "BG.Play"):
            self._playing = True

    def pause(self) -> None:
        since = time.time() - self._play_at
        if since < _PLAY_GRACE_S:
            log(f"[tt] ignoring pause {since*1000:.0f}ms after play "
                f"(stale teardown of the previous session, not a real stop)")
            return
        self._stop_audio()
        if self._send(self._op_stop, "Sys.Standby"):
            self._playing = False
            self._grace_until = time.time() + _ECHO_GRACE_S
        if self._adc_setup:
            # Park the PGA at 0 dB like the capture script does, so a
            # stray later capture isn't surprised by +36 dB of gain.
            self._i2c(_REG_PGA_L, _pga_reg(0.0))
            self._i2c(_REG_PGA_R, _pga_reg(0.0))

    def next(self) -> None:
        self._send(self._op_next, "BG.ADV->")

    def prev(self) -> None:
        self._send(self._op_prev, "BG.<-Ret")

    # ---- introspection -----------------------------------------------------

    def is_playing(self) -> bool:
        return self._playing

    def metadata(self) -> Optional[Metadata]:
        """A record player knows nothing about what's on the record, but the
        B&O front panel still wants *something* -- without it the display
        keeps the role's "Connecting" placeholder. So we report a fixed
        title (default "PHONO", set `metadata_title` to change it)."""
        if not self._title:
            return None
        return Metadata(title=self._title, artist="", album="", genre="")

    # ---- DL'80 plumbing ----------------------------------------------------

    def _send(self, opcode: int, what: str) -> bool:
        """Publish one DL'80 opcode, debounced. True if it went out."""
        now = time.time()
        with self._lock:
            if now - self._last_sent.get(opcode, 0.0) < _TRANSPORT_DEBOUNCE_S:
                return False
            self._last_sent[opcode] = now
        try:
            self._r.publish(DL80_TX, f"{opcode:02x}")
            log(f"[tt] DL'80 -> 0x{opcode:02x} {what}")
            return True
        except redis.exceptions.RedisError as e:
            log(f"[tt] redis publish failed: {e}", err=True)
            return False

    def _snoop(self) -> None:
        """Track the Beogram's own status echoes so is_playing() reflects
        the hardware, including when someone uses the turntable's own
        buttons instead of the ML remote."""
        ps = None
        while not self._snoop_stop.is_set():
            try:
                if ps is None:
                    ps = self._r.pubsub()
                    ps.subscribe(DL80_RX)
                m = ps.get_message(timeout=0.5,
                                   ignore_subscribe_messages=True)
                if m is None:
                    continue
                data = m.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                try:
                    op = int(str(data).strip(), 16) & 0xFF
                except ValueError:
                    continue
                if op in self._st_play:
                    if time.time() < self._grace_until:
                        continue          # coasting after our stop; ignore
                    if not self._playing:
                        log(f"[tt] Beogram reports playing "
                            f"(0x{op:02x}) -- following it")
                    self._playing = True
                elif op in self._st_stop:
                    if self._playing:
                        log(f"[tt] Beogram reports stopped (0x{op:02x})")
                    self._playing = False
                    self._stop_audio()
            except redis.exceptions.RedisError as e:
                log(f"[tt] snoop redis error: {e}", err=True)
                if ps is not None:
                    try: ps.close()
                    except Exception: pass
                    ps = None
                self._snoop_stop.wait(1.0)
        if ps is not None:
            try: ps.close()
            except Exception: pass

    # ---- ADC front end -----------------------------------------------------

    def _i2c(self, reg: int, val: int) -> None:
        cmd = [self._i2cset, "-f", "-y", str(self._i2c_bus), self._i2c_addr,
               f"0x{reg:02x}", f"0x{val:02x}", "b"]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=5)
        except FileNotFoundError:
            log(f"[tt] {self._i2cset} not found (apt install i2c-tools) -- "
                f"skipping ADC setup", err=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log(f"[tt] i2cset {cmd[-3]}={cmd[-2]} failed: {e}", err=True)

    def _configure_adc(self, pga_db: float) -> None:
        """Point ADC1 L/R at the phono input and set the PGA gain."""
        self._i2c(_REG_ADC1L_IN, self._adc_input)
        self._i2c(_REG_ADC1R_IN, self._adc_input)
        reg = _pga_reg(pga_db)
        self._i2c(_REG_PGA_L, reg)
        self._i2c(_REG_PGA_R, reg)
        log(f"[tt] ADC: input=0x{self._adc_input:02x} "
            f"PGA={pga_db:+g}dB (0x{reg:02x})")

    # ---- audio loopback ----------------------------------------------------

    def _bits(self) -> int:
        return 16 if "16" in self._format else 32

    def _pipeline(self) -> str:
        """ADC -> [RIAA] -> DAC as a shell pipeline."""
        common = (f"-f {self._format} -r {self._rate} -c {self._channels}")
        period = f" --period-size={self._period}" if self._period else ""
        rec = f"arecord -q -D {self._cap_dev} {common}{period}"
        play = f"aplay -q -D {self._play_dev} {common}{period}"
        if not self._riaa:
            return f"{rec} | {play}"
        riaa = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "riaa_stream.py")
        filt = (f"python3 -u {riaa} --rate {self._rate} "
                f"--channels {self._channels} --bits {self._bits()} "
                f"--hp {self._riaa_hp} --gain-db {self._riaa_gain}")
        return f"{rec} | {filt} | {play}"

    def _start_audio(self) -> None:
        if not self._loopback:
            return
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return                              # already running
            cmd = self._pipeline()
            try:
                # Own process group so we can kill the whole pipeline.
                self._proc = subprocess.Popen(
                    cmd, shell=True, start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except Exception as e:
                log(f"[tt] failed to start audio loopback: {e}", err=True)
                self._proc = None
                return
        log(f"[tt] audio loopback up: {cmd}")

    def _stop_audio(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        # Surface why it died if it wasn't us (bad device name, busy card).
        try:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
        except Exception:
            err = ""
        if err:
            log(f"[tt] loopback stderr: {err.splitlines()[-1]}", err=True)
        log("[tt] audio loopback down")
